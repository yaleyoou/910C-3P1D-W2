"""Run on P1 host; measure wall-clock offset on the D host using SSH."""
import json,subprocess,sys,time
from pathlib import Path
rows=[]
for _ in range(7):
    a=time.time_ns()
    v=subprocess.check_output(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5',sys.argv[1],'date +%s%N'])
    b=time.time_ns()
    rows.append(dict(p_send=a,d_clock=int(v),p_receive=b))
Path(sys.argv[2]).write_text(json.dumps(rows))

