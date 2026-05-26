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

def extract_python_nodes(file_path: str, content: str, repo_id: str, db: Session) -> list:
    nodes = []
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                code_node = CodeNode(
                    repo_id=repo_id,
                    node_type=NodeType.function,
                    name=node.name,
                    file_path=file_path,
                    content=ast.get_source_segment(content, node) or ""
                )
                db.add(code_node)
                nodes.append(code_node)
            elif isinstance(node, ast.ClassDef):
                code_node = CodeNode(
                    repo_id=repo_id,
                    node_type=NodeType.class_,
                    name=node.name,
                    file_path=file_path,
                    content=ast.get_source_segment(content, node) or ""
                )
                db.add(code_node)
                nodes.append(code_node)
    except Exception as e:
        logger.warning(f"Could not parse {file_path}: {e}")
    return nodes

def extract_imports_edges(file_path: str, content: str, file_node: CodeNode, all_nodes: list, db: Session):
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    module = node.names[0].name if node.names else ""

                # Find matching file node
                for target_node in all_nodes:
                    if target_node.node_type == NodeType.file and module in target_node.file_path.replace("/", ".").replace(".py", ""):
                        edge = CodeEdge(
                            source_id=file_node.id,
                            target_id=target_node.id,
                            edge_type=EdgeType.imports
                        )
                        db.add(edge)
                        break
    except Exception:
        pass

def index_repository(repo_id: str, access_token: str):
    db = SessionLocal()
    try:
        repo = db.query(Repository).filter(Repository.id == repo_id).first()
        if not repo:
            return

        logger.info(f"Starting indexing for {repo.repo_full_name}")

        # Clear old nodes
        old_nodes = db.query(CodeNode).filter(CodeNode.repo_id == repo_id).all()
        for n in old_nodes:
            db.query(CodeEdge).filter(
                (CodeEdge.source_id == n.id) | (CodeEdge.target_id == n.id)
            ).delete()
        db.query(CodeNode).filter(CodeNode.repo_id == repo_id).delete()
        db.commit()

        # Fetch all files from GitHub
        try:
            files = get_repo_contents(access_token, repo.repo_full_name)
        except Exception as e:
            logger.error(f"Failed to fetch repo contents: {e}")
            return

        all_nodes = []

        # First pass — create file nodes and extract functions/classes
        for file in files:
            if not file["path"].endswith((".py", ".js", ".ts", ".jsx", ".tsx")):
                continue
            if len(file.get("content", "")) == 0:
                continue

            # File node
            file_node = CodeNode(
                repo_id=repo_id,
                node_type=NodeType.file,
                name=file["path"].split("/")[-1],
                file_path=file["path"],
                content=file["content"][:4000]
            )
            db.add(file_node)
            db.flush()
            all_nodes.append(file_node)

            # Extract functions/classes for Python
            if file["path"].endswith(".py"):
                child_nodes = extract_python_nodes(file["path"], file["content"], repo_id, db)
                all_nodes.extend(child_nodes)

        db.flush()

        # Second pass — extract import edges
        for file in files:
            if not file["path"].endswith(".py"):
                continue
            file_node = next((n for n in all_nodes if n.file_path == file["path"] and n.node_type == NodeType.file), None)
            if file_node:
                extract_imports_edges(file["path"], file["content"], file_node, all_nodes, db)

        db.commit()

        # Store all nodes in Pinecone
        for node in all_nodes:
            pinecone_id = store_node_in_pinecone(node)
            if pinecone_id:
                node.pinecone_id = pinecone_id
        
        db.commit()

        repo.is_indexed = True
        db.commit()
        logger.info(f"Finished indexing {repo.repo_full_name} — {len(all_nodes)} nodes")

    except Exception as e:
        logger.error(f"Indexing failed for repo {repo_id}: {e}")
    finally:
        db.close()

def index_repository_background(repo_id: str, access_token: str):
    thread = threading.Thread(target=index_repository, args=(repo_id, access_token), daemon=True)
    thread.start()