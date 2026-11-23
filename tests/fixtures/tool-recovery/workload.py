"""Synthetic log-producing workload, not a replacement for a native tool."""
from pathlib import Path

body = '{"recovery_key":"quartz","count":37}\n' + 'ordinary log detail\n' * 500
Path('spill.log').write_text(body)
print('SYNTHETIC_PREVIEW_TRUNCATED full_output=spill.log')
print('Only a preview is available here; inspect the spill file for the result.')
