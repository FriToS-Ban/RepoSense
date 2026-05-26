import logging
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
            dimension=768,
            metric="cosine",
            spec={"serverless": {"cloud": "aws", "region": "us-east-1"}}
        )
    return pc.Index(settings.PINECONE_INDEX_NAME)

def embed_text(text: str) -> List[float]:
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    result = client.models.embed_content(
        model="text-embedding-004",
        contents=text
    )
    return result.embeddings[0].values

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

def get_relevant_context(repo_id: str, diff: str, db: Session, top_k: int = 5) -> str:
    try:
        index = get_pinecone_index()
        query_vector = embed_text(diff[:8000])
        results = index.query(
            vector=query_vector,
            top_k=top_k,
            filter={"repo_id": repo_id},
            include_metadata=True
        )
        if not results.matches:
            return ""

        context_parts = []
        for match in results.matches:
            node_id = match.metadata.get("node_id")
            node = db.query(CodeNode).filter(CodeNode.id == node_id).first()
            if not node:
                continue

            context_parts.append(f"=== {node.file_path} ({node.node_type.value}: {node.name}) ===")
            context_parts.append(node.content or "")

            # Traverse graph — add related nodes
            for edge in node.outgoing_edges[:3]:
                related = db.query(CodeNode).filter(CodeNode.id == edge.target_id).first()
                if related:
                    context_parts.append(f"--- Related ({edge.edge_type.value}): {related.file_path} ---")
                    context_parts.append((related.content or "")[:500])

        return "\n\n".join(context_parts)

    except Exception as e:
        logger.error(f"Failed to get RAG context: {e}")
        return ""