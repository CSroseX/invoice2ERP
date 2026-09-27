import logging
import json
import logging.handlers
from pathlib import Path
from datetime import datetime, timezone

# Ensure logs directory exists
log_dir = Path("logs")
log_dir.mkdir(parents=True, exist_ok=True)

class JsonFormatter(logging.Formatter):
    """
    Format log records as structured JSON for Datadog / Splunk ingest
    """
    def format(self, record):
        # Base JSON payload structure
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
        }
        
        # Include custom structured data if passed in the `extra` parameter
        # Example: logger.info("doc processed", extra={"trace_id": "123", "filename": "x.pdf"})
        if hasattr(record, "trace_id"):
            log_entry["trace_id"] = record.trace_id
        if hasattr(record, "doc_filename"):
            log_entry["document_filename"] = record.doc_filename
        if hasattr(record, "status"):
            log_entry["status"] = record.status
            
        # Exception details
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
            
        return json.dumps(log_entry)

def get_audit_logger(name: str) -> logging.Logger:
    """
    Returns an audit logger configured to output append-only JSONL files
    suitable for production log aggregation, plus a console stream.
    """
    logger = logging.getLogger(name)
    
    # Prevent duplicate handlers if called multiple times
    if logger.hasHandlers():
        return logger
        
    logger.setLevel(logging.INFO)
    logger.propagate = False
    
    # 1. Immutable Audit Trail JSONL Handler (Rotates at 10MB)
    audit_handler = logging.handlers.RotatingFileHandler(
        log_dir / "audit.jsonl",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8"
    )
    audit_handler.setFormatter(JsonFormatter())
    
    import sys
    # 2. Console Stream Handler (For local console & Streamlit capture)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    
    logger.addHandler(audit_handler)
    logger.addHandler(console_handler)
    
    return logger
