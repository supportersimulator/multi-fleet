"""Cross-product adapters — the integration glue between all 5 ContextDNA products.

Each adapter translates between product-native formats so the products
can communicate through the fleet nervous system.

Adapters:
  contextdna/chief_sync     — pull ContextDNA memory before major work
  contextdna/packet_delivery — attach memory packets to routed tasks
  three_surgeons/surgeon_events — convert surgeon phases to fleet events
  three_surgeons/surgeon_escalation — build escalation packets on no-consensus
  superpowers/brainstorm_ingest — normalize brainstorm output to packet
  superset/overlay_stream — publish state to Superset overlays
"""
