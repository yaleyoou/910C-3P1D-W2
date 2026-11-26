import signal
signal.signal(signal.SIGTERM, lambda *_: exit(0))
import argparse
import gzip
import json
import time
from urllib.request import ProxyHandler, build_opener

parser = argparse.ArgumentParser()
parser.add_argument('endpoint')
parser.add_argument('output')
parser.add_argument('--seconds', type=float, default=10)
args = parser.parse_args()
client = build_opener(ProxyHandler({}))
previous = set()
with gzip.open(args.output, 'xt') as stream:
    while True:
        start = time.time_ns()
        try:
            with client.open(args.endpoint, timeout=8) as response:
                keys = set(response.read().decode().splitlines())
            end = time.time_ns()
            row = dict(begin_ns=start, end_ns=end, count=len(keys), added=sorted(keys-previous), removed=sorted(previous-keys))
            previous = keys
        except OSError as error:
            row = dict(begin_ns=start, end_ns=time.time_ns(), error=str(error))
        stream.write(json.dumps(row)+'\n')
        stream.flush()
        time.sleep(max(0, args.seconds-(time.time_ns()-start)/1e9))
