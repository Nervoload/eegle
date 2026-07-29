# LSL observe-only

The normalized design is portable and initially includes a deterministic
simulation deployment. Live LSL discovery writes a separate review-only
proposal; it never rewrites the suite or infers action authority.

```bash
python -m pip install "eegle[live]"
eegle detect . --lsl --propose \
  --select-source requirement.source.neural=YOUR_CAPABILITY_ID
eegle compile . --deployment deployment_proposal
eegle preflight .
eegle run .
```
