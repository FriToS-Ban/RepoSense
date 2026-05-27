import ast
import logging
import re
import threading
from sqlalchemy.orm import Session

from backend.core.database import SessionLocal
from backend.models.models import CodeNode, CodeEdge, EdgeType, NodeType, Repository
from backend.services.rag import store_node_in_pinecone
from backend.services.github import get_repo_contents

logger = logging.getLogger(__name__)

# ─── Improvement 1: Smart chunking ───────────────────────────────────────────

CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200

def chunk_content(content: str) -> list[str]:
    """Split large content into overlapping chunks."""
    if len(content) <= CHUNK_SIZE:
        return [content]
    chunks = []
    start = 0
    while start < len(content):
        end = start + CHUNK_SIZE
        chunks.append(content[start:end])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks

# ─── Improvement 2: Python parsing ───────────────────────────────────────────

def extract_python_nodes(file_path: str, content: str, repo_id: str, db: Session) -> list:
    nodes = []
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                source = ast.get_source_segment(content, node) or ""
                for i, chunk in enumerate(chunk_content(source)):
                    code_node = CodeNode(
                        repo_id=repo_id,
                        node_type=NodeType.function,
                        name=f"{node.name}__chunk{i}" if i > 0 else node.name,
                        file_path=file_path,
                        content=chunk
                    )
                    db.add(code_node)
                    nodes.append(code_node)
            elif isinstance(node, ast.ClassDef):
                source = ast.get_source_segment(content, node) or ""
                for i, chunk in enumerate(chunk_content(source)):
                    code_node = CodeNode(
                        repo_id=repo_id,
                        node_type=NodeType.class_,
                        name=f"{node.name}__chunk{i}" if i > 0 else node.name,
                        file_path=file_path,
                        content=chunk
                    )
                    db.add(code_node)
                    nodes.append(code_node)
    except Exception as e:
        logger.warning(f"Could not parse Python file {file_path}: {e}")
    return nodes

# ─── Improvement 2: JS/TS parsing ────────────────────────────────────────────

JS_FUNCTION_PATTERNS = [
    re.compile(r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\('),           # function foo()
    re.compile(r'(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\('),          # const foo = () =>
    re.compile(r'(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?function'),    # const foo = function
]

JS_CLASS_PATTERN = re.compile(r'(?:export\s+)?class\s+(\w+)')

def extract_js_nodes(file_path: str, content: str, repo_id: str, db: Session) -> list:
    nodes = []
    lines = content.split('\n')

    def extract_block(start_line: int) -> str:
        """Extract a code block starting from a line by tracking braces."""
        block_lines = []
        depth = 0
        started = False
        for line in lines[start_line:start_line + 100]:
            block_lines.append(line)
            depth += line.count('{') - line.count('}')
            if depth > 0:
                started = True
            if started and depth <= 0:
                break
        return '\n'.join(block_lines)

    for i, line in enumerate(lines):
        # Functions
        for pattern in JS_FUNCTION_PATTERNS:
            match = pattern.search(line)
            if match:
                name = match.group(1)
                source = extract_block(i)
                for j, chunk in enumerate(chunk_content(source)):
                    code_node = CodeNode(
                        repo_id=repo_id,
                        node_type=NodeType.function,
                        name=f"{name}__chunk{j}" if j > 0 else name,
                        file_path=file_path,
                        content=chunk
                    )
                    db.add(code_node)
                    nodes.append(code_node)
                break

        # Classes
        match = JS_CLASS_PATTERN.search(line)
        if match:
            name = match.group(1)
            source = extract_block(i)
            for j, chunk in enumerate(chunk_content(source)):
                code_node = CodeNode(
                    repo_id=repo_id,
                    node_type=NodeType.class_,
                    name=f"{name}__chunk{j}" if j > 0 else name,
                    file_path=file_path,
                    content=chunk
                )
                db.add(code_node)
                nodes.append(code_node)

    return nodes

# ─── Improvement 3: Edge detection ───────────────────────────────────────────

def extract_python_edges(file_path: str, content: str, file_node: CodeNode, all_nodes: list, db: Session):
    try:
        tree = ast.parse(content)

        # Import edges
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    module = node.names[0].name if node.names else ""
                for target_node in all_nodes:
                    if target_node.node_type == NodeType.file and module in target_node.file_path.replace("/", ".").replace(".py", ""):
                        db.add(CodeEdge(source_id=file_node.id, target_id=target_node.id, edge_type=EdgeType.imports))
                        break

        # Call edges — find function calls and match to known function nodes
        func_names = {n.name.split("__chunk")[0]: n for n in all_nodes if n.node_type == NodeType.function and n.file_path != file_path}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name and func_name in func_names:
                    db.add(CodeEdge(source_id=file_node.id, target_id=func_names[func_name].id, edge_type=EdgeType.calls))

        # Inheritance edges
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_node = next((n for n in all_nodes if n.node_type == NodeType.class_ and n.name == node.name and n.file_path == file_path), None)
                if not class_node:
                    continue
                for base in node.bases:
                    base_name = base.id if isinstance(base, ast.Name) else None
                    if base_name:
                        base_node = next((n for n in all_nodes if n.node_type == NodeType.class_ and n.name.split("__chunk")[0] == base_name), None)
                        if base_node:
                            db.add(CodeEdge(source_id=class_node.id, target_id=base_node.id, edge_type=EdgeType.inherits))

    except Exception as e:
        logger.warning(f"Could not extract edges from {file_path}: {e}")

def extract_js_edges(file_path: str, content: str, file_node: CodeNode, all_nodes: list, db: Session):
    try:
        # JS import edges
        import_patterns = [
            re.compile(r'import\s+.*?\s+from\s+[\'"](.+?)[\'"]'),
            re.compile(r'require\([\'"](.+?)[\'"]\)'),
        ]
        for pattern in import_patterns:
            for match in pattern.finditer(content):
                module_path = match.group(1)
                for target_node in all_nodes:
                    if target_node.node_type == NodeType.file:
                        target_clean = target_node.file_path.replace('.js', '').replace('.ts', '').replace('.jsx', '').replace('.tsx', '')
                        if module_path.replace('./', '') in target_clean or target_clean.endswith(module_path.lstrip('./')):
                            db.add(CodeEdge(source_id=file_node.id, target_id=target_node.id, edge_type=EdgeType.imports))
                            break

        # JS function call edges
        call_pattern = re.compile(r'\b(\w+)\s*\(')
        func_names = {n.name.split("__chunk")[0]: n for n in all_nodes if n.node_type == NodeType.function and n.file_path != file_path}
        for match in call_pattern.finditer(content):
            name = match.group(1)
            if name in func_names:
                db.add(CodeEdge(source_id=file_node.id, target_id=func_names[name].id, edge_type=EdgeType.calls))

    except Exception as e:
        logger.warning(f"Could not extract JS edges from {file_path}: {e}")

# ─── Core indexing ────────────────────────────────────────────────────────────

def index_files(repo_id: str, files: list, db: Session, clear_first: bool = True):
    """Index a list of files into the knowledge graph."""
    if clear_first:
        old_nodes = db.query(CodeNode).filter(CodeNode.repo_id == repo_id).all()
        for n in old_nodes:
            db.query(CodeEdge).filter(
                (CodeEdge.source_id == n.id) | (CodeEdge.target_id == n.id)
            ).delete()
        db.query(CodeNode).filter(CodeNode.repo_id == repo_id).delete()
        db.commit()

    all_nodes = []

    # First pass — file nodes + function/class extraction
    for file in files:
        if not file["path"].endswith((".py", ".js", ".ts", ".jsx", ".tsx")):
            continue
        content = file.get("content", "")
        if not content:
            continue

        # File node — chunked if large
        for i, chunk in enumerate(chunk_content(content)):
            file_node = CodeNode(
                repo_id=repo_id,
                node_type=NodeType.file,
                name=f"{file['path'].split('/')[-1]}__chunk{i}" if i > 0 else file['path'].split('/')[-1],
                file_path=file["path"],
                content=chunk
            )
            db.add(file_node)
            db.flush()
            if i == 0:
                all_nodes.append(file_node)  # only first chunk used for edge detection

        # Extract functions/classes
        if file["path"].endswith(".py"):
            child_nodes = extract_python_nodes(file["path"], content, repo_id, db)
            all_nodes.extend(child_nodes)
        elif file["path"].endswith((".js", ".ts", ".jsx", ".tsx")):
            child_nodes = extract_js_nodes(file["path"], content, repo_id, db)
            all_nodes.extend(child_nodes)

    db.flush()

    # Second pass — edges
    for file in files:
        content = file.get("content", "")
        if not content:
            continue
        file_node = next((n for n in all_nodes if n.file_path == file["path"] and n.node_type == NodeType.file), None)
        if not file_node:
            continue
        if file["path"].endswith(".py"):
            extract_python_edges(file["path"], content, file_node, all_nodes, db)
        elif file["path"].endswith((".js", ".ts", ".jsx", ".tsx")):
            extract_js_edges(file["path"], content, file_node, all_nodes, db)

    db.commit()

    # Store in Pinecone
    for node in all_nodes:
        pinecone_id = store_node_in_pinecone(node)
        if pinecone_id:
            node.pinecone_id = pinecone_id
    db.commit()

    return all_nodes

def index_repository(repo_id: str, access_token: str):
    db = SessionLocal()
    try:
        repo = db.query(Repository).filter(Repository.id == repo_id).first()
        if not repo:
            return
        logger.info(f"Starting full indexing for {repo.repo_full_name}")
        files = get_repo_contents(access_token, repo.repo_full_name)
        all_nodes = index_files(repo_id, files, db, clear_first=True)
        repo.is_indexed = True
        db.commit()
        logger.info(f"Finished indexing {repo.repo_full_name} — {len(all_nodes)} nodes")
    except Exception as e:
        logger.error(f"Indexing failed for repo {repo_id}: {e}")
    finally:
        db.close()

# ─── Improvement 4: Re-index changed files on push ───────────────────────────

def reindex_changed_files(repo_id: str, access_token: str, changed_file_paths: list[str]):
    """Re-index only the files that changed in a push event."""
    db = SessionLocal()
    try:
        repo = db.query(Repository).filter(Repository.id == repo_id).first()
        if not repo or not repo.is_indexed:
            return

        logger.info(f"Re-indexing {len(changed_file_paths)} changed files in {repo.repo_full_name}")

        # Remove old nodes for changed files only
        for file_path in changed_file_paths:
            old_nodes = db.query(CodeNode).filter(
                CodeNode.repo_id == repo_id,
                CodeNode.file_path == file_path
            ).all()
            for n in old_nodes:
                db.query(CodeEdge).filter(
                    (CodeEdge.source_id == n.id) | (CodeEdge.target_id == n.id)
                ).delete()
                db.delete(n)
        db.commit()

        # Fetch only changed files from GitHub
        from backend.services.github import get_file_content
        files = []
        for path in changed_file_paths:
            content = get_file_content(access_token, repo.repo_full_name, path)
            if content:
                files.append({"path": path, "content": content})

        if files:
            index_files(repo_id, files, db, clear_first=False)
            logger.info(f"Re-indexed {len(files)} files for {repo.repo_full_name}")

    except Exception as e:
        logger.error(f"Re-indexing failed for repo {repo_id}: {e}")
    finally:
        db.close()

def index_repository_background(repo_id: str, access_token: str):
    thread = threading.Thread(target=index_repository, args=(repo_id, access_token), daemon=True)
    thread.start()

def reindex_changed_files_background(repo_id: str, access_token: str, changed_file_paths: list[str]):
    thread = threading.Thread(target=reindex_changed_files, args=(repo_id, access_token, changed_file_paths), daemon=True)
    thread.start()