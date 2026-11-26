import json, sys
from pathlib import Path
root=Path(sys.argv[1])
def bounds(file):
    records=json.loads(file.read_text())
    best=min(records,key=lambda r:r['p_receive']-r['p_send'])
    return [(best['d_clock']-best[k])/1e9 for k in ('p_receive','p_send')]
a,b=[bounds(root/f'clock-{name}.json') for name in ('before','after')]
print('D-P1 clock offset bounds (s)',a,b)
if a[1]-a[0]>1 or b[1]-b[0]>1 or max(a[0]-b[1],b[0]-a[1])>0.25:
    raise ValueError('clock probes too uncertain or drifted; do not aggregate overlap')
