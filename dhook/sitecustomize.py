import importlib.util
import sys
from pathlib import Path

class OverlapModule:
    def find_spec(self, fullname, path, target=None):
        if fullname.endswith('.kv_p2p.sfa_pd_rd2h.scheduler'):
            return importlib.util.spec_from_file_location(fullname, Path(__file__).with_name('scheduler.py'))

sys.meta_path.insert(0, OverlapModule())
