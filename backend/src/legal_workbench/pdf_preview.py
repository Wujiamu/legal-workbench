"""Serialize PDFium access within the HTTP and MCP processes."""
from threading import Lock

pdf_lock = Lock()
