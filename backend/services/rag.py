import logging
import re
from typing import List, Optional
from sqlalchemy.orm import Session
from pinecone import Pinecone
from google import genai

from backend.core.config import settings
from backend.models.models import CodeNode, CodeEdge, EdgeType, NodeType, Repository

logger = logging.getLogger(__name__)

def get_pinecone_index():
    pc = Pinecone(api_key=settings.PINECONE_API_KEY)
    existing = [i.name for i in pc.list_indexes()]
    if settings.PINECONE_INDEX_NAME not in existing:
        pc.create_index(
            name=settings.PINECONE_INDEX_NAME,
            dimension=1024,
            metric="cosine",
            spec={"serverless": {"cloud": "aws", "region": "us-east-1"}}
        )
    return pc.Index(settings.PINECONE_INDEX_NAME)

def embed_text(text: str) -> List[float]:
    from openai import OpenAI
    client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=settings.NVIDIA_API_KEY
    )
    response = client.embeddings.create(
        model="nvidia/nv-embedqa-e5-v5",
        input=text[:8000],
        encoding_format="float",
        extra_body={"input_type": "query", "truncate": "END"}
    )
    return response.data[0].embedding

def store_node_in_pinecone(node: CodeNode) -> Optional[str]:
    try:
        index = get_pinecone_index()
        text = f"{node.file_path}\n{node.name}\n{node.content or ''}"
        vector = embed_text(text[:8000])
        pinecone_id = f"{node.repo_id}_{node.id}"
        index.upsert(vectors=[{
            "id": pinecone_id,
            "values": vector,
            "metadata": {
                "repo_id": node.repo_id,
                "node_id": node.id,
                "file_path": node.file_path,
                "name": node.name,
                "node_type": node.node_type.value
            }
        }])
        return pinecone_id
    except Exception as e:
        logger.error(f"Failed to store node in Pinecone: {e}")
        return None

# ─── Improvement 6: Extract individual changed functions from diff ────────────

def extract_diff_chunks(diff: str) -> List[str]:
    """
    Split a diff into individual changed function/class chunks.
    Each hunk in the diff is a separate query rather than embedding the whole diff.
    """
    chunks = []
    current_file = ""
    current_chunk = []

    for line in diff.split('\n'):
        if line.startswith('diff --git'):
            if current_chunk:
                chunks.append((current_file, '\n'.join(current_chunk)))
            current_chunk = []
            # Extract filename from diff header
            parts = line.split(' b/')
            current_file = parts[-1] if len(parts) > 1 else ""
        elif line.startswith('@@'):
            if current_chunk:
                chunks.append((current_file, '\n'.join(current_chunk)))
            current_chunk = [line]
        else:
            current_chunk.append(line)

    if current_chunk:
        chunks.append((current_file, '\n'.join(current_chunk)))

    # Filter out empty or too-small chunks
    return [(f, c) for f, c in chunks if len(c.strip()) > 50]


# ─── Improvement 7: Deduplication ────────────────────────────────────────────

def deduplicate_nodes(nodes: List[CodeNode]) -> List[CodeNode]:
    """
    Remove duplicate nodes by file_path + name combination.
    Keeps the node with the most content.
    """
    seen = {}
    for node in nodes:
        key = f"{node.file_path}::{node.name.split('__chunk')[0]}"
        if key not in seen:
            seen[key] = node
        else:
            # Keep whichever has more content
            if len(node.content or "") > len(seen[key].content or ""):
                seen[key] = node
    return list(seen.values())


# ─── Improved context retrieval ───────────────────────────────────────────────

def get_relevant_context(repo_id: str, diff: str, db: Session, top_k: int = 5, changed_file_paths: list = []) -> str:
    try:
        index = get_pinecone_index()

        # Improvement 6: embed each diff chunk separately for targeted retrieval
        diff_chunks = extract_diff_chunks(diff)

        if not diff_chunks:
            # Fallback to embedding full diff if no chunks found
            diff_chunks = [("", diff[:8000])]
        
        # Always include directly changed files first
        direct_nodes = []
        for file_path in changed_file_paths:
            nodes = db.query(CodeNode).filter(
                CodeNode.repo_id == repo_id,
                CodeNode.file_path == file_path
            ).all()
            direct_nodes.extend(nodes)
        all_matched_nodes.extend(direct_nodes)

        all_matched_nodes: List[CodeNode] = []

        for file_path, chunk_text in diff_chunks[:8]:  # limit to 8 chunks max
            try:
                query_vector = embed_text(chunk_text[:8000])
                results = index.query(
                    vector=query_vector,
                    top_k=top_k,
                    filter={"repo_id": repo_id},
                    include_metadata=True
                )
                for match in results.matches:
                    node_id = match.metadata.get("node_id")
                    node = db.query(CodeNode).filter(CodeNode.id == node_id).first()
                    if node:
                        all_matched_nodes.append(node)

                        # Graph traversal — add related nodes
                        for edge in node.outgoing_edges[:3]:
                            related = db.query(CodeNode).filter(CodeNode.id == edge.target_id).first()
                            if related:
                                all_matched_nodes.append(related)

            except Exception as e:
                logger.warning(f"Failed to embed chunk for {file_path}: {e}")
                continue

        if not all_matched_nodes:
            return ""

        # Improvement 7: deduplicate before building context
        unique_nodes = deduplicate_nodes(all_matched_nodes)

        # Sort by node type — files last, functions/classes first (more specific)
        unique_nodes.sort(key=lambda n: 0 if n.node_type != NodeType.file else 1)

        # Build context string with a budget of 12000 chars total
        context_parts = []
        total_chars = 0
        char_budget = 12000

        for node in unique_nodes:
            content = node.content or ""
            header = f"=== {node.file_path} ({node.node_type.value}: {node.name.split('__chunk')[0]}) ==="
            entry = f"{header}\n{content}"

            if total_chars + len(entry) > char_budget:
                # Add truncated version if there's still some budget left
                remaining = char_budget - total_chars
                if remaining > 100:
                    context_parts.append(entry[:remaining] + "\n[truncated]")
                break

            context_parts.append(entry)
            total_chars += len(entry)

        return "\n\n".join(context_parts)

    except Exception as e:
        logger.error(f"Failed to get RAG context: {e}")
        return ""