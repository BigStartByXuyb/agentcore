"""Memory system — cross-session recall and extraction.

Public API:
  - paths:   get_memory_dir(), ensure_memory_dir(), is_memory_path()
  - scan:    scan_memory_files(), format_memory_manifest()
  - prompt:  build_memory_prompt()
  - recall:  find_relevant_memories()
  - extract: run_memory_extraction()
"""
