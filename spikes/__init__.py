"""ATV-bench spike package.

Two gating spikes from the design doc:
  spike_copilot_headless  -> can a harness CLI be driven headless (no TTY,
                             token via env), producing a code edit?
  spike_codeclash_decoupling -> does CodeClash's match engine accept an
                             externally-authored bot independent of its model
                             layer? (Player.run seam)

Both are validated by tests/ under TDD.
"""
