"""Actual installed Matplotlib CLI tests; explicit fixture JSON, no business data."""
import json
import sys
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
PYTHON=Path(sys.executable)
SCRIPT=ROOT/'scripts/plot_market.py'


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "optional Matplotlib dependency not installed")
class PlotReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.data=dict(month='2026-10',status='candidate_unreviewed',data_mode='FIXTURE_SYNTHETIC',as_of='2026-10-30T18:00:00+08:00',series={k:{} for k in ('sse','shenzhen','chinext')})
    def run_plot(self,name='output.png'):
        inp=self.root/'chart.json';inp.write_text(json.dumps(self.data))
        return subprocess.run([str(PYTHON),str(SCRIPT),str(inp),str(self.root/name)],capture_output=True,text=True,timeout=60)
    def test_empty_month_is_rendered_with_actual_library(self):
        result=self.run_plot();self.assertEqual(result.returncode,0,result.stderr)
        meta=json.loads(result.stdout);self.assertEqual(meta['observations'],0);self.assertEqual(meta['panels'],3);self.assertEqual(meta['dependency'],'matplotlib')
        self.assertEqual((self.root/'output.png').read_bytes()[:8],b'\x89PNG\r\n\x1a\n')
    def test_source_backed_fixture_and_exclusive_output(self):
        self.data['series']['sse']['2026-10-30']=dict(value='3000.12',unit='点',sources=[dict(source_id='fixture',url='https://www.pbc.gov.cn/fixture',locator='FIXTURE line')])
        result=self.run_plot();self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout)['observations'],1)
        before=(self.root/'output.png').read_bytes();again=self.run_plot();self.assertNotEqual(again.returncode,0);self.assertEqual((self.root/'output.png').read_bytes(),before)
    def test_missing_provenance_and_nan_do_not_create_plots(self):
        for record in [dict(value='1',unit='点',sources=[]),dict(value='NaN',unit='点',sources=[{'url':'fixture'}])]:
            self.data['series']['sse']={'2026-10-30':record}
            result=self.run_plot();self.assertNotEqual(result.returncode,0);self.assertFalse((self.root/'output.png').exists())
