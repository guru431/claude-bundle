Analyze the following healthcheck metrics from one or more hosts.

Report:
- anomalies (unusual load, swapping, runaway processes)
- low disk space (flag anything above 85% usage explicitly)
- memory pressure
- anything that looks misconfigured or missing compared to a healthy host

Rules:
- Be concise: a short verdict line first ("OK" / "ATTENTION: ..."), then
  one bullet per issue.
- If everything looks normal, say so in one line — do not invent issues.
- Numbers over prose: quote the exact metric that triggered each concern.
