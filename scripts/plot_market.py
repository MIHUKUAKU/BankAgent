"""Render retained market chart JSON with Matplotlib (no DB/network access).

Based on Matplotlib's official simple_plot example (Figure/Axes.plot pattern):
https://matplotlib.org/stable/gallery/lines_bars_and_markers/simple_plot.html
No example data or copied bespoke renderer. Reuses the installed plotting library.
"""
import argparse
import calendar
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import tempfile
import textwrap

LABELS = {'sse':'Shanghai Composite (historical scope)', 'shenzhen':'Shenzhen Component (historical scope)',
          'chinext':'ChiNext (historical scope)', 'csi300':'CSI 300', 'hsi':'Hang Seng', 'nikkei225':'Nikkei 225',
          'nasdaq':'NASDAQ Composite', 'djia':'Dow Jones Industrial Average', 'sp500':'S&P 500', 'eurostoxx50':'EURO STOXX 50'}


def plot_market(input_path, output_path):
    raw = Path(input_path).read_bytes()
    data = json.loads(raw)
    month = data['month']
    start = date.fromisoformat(month + '-01')
    if start.strftime('%Y-%m') != month or data.get('status') not in ('published','candidate_unreviewed'):
        raise ValueError('explicit month and published/candidate_unreviewed status required')
    series = data.get('series')
    if not isinstance(series,dict) or not series:
        raise ValueError('series mapping required; use explicit empty panels for missing data')
    sources = set()
    panels = []
    for key, observations in series.items():
        if not isinstance(observations,dict):
            raise ValueError('dated observation mapping required')
        points, units = [], set()
        for day, record in sorted(observations.items()):
            if not isinstance(day,str) or not day.startswith(month):
                raise ValueError('observation outside chart month')
            stamp = datetime.fromisoformat(day) if 'T' in day else datetime.combine(date.fromisoformat(day),datetime.min.time())
            value = Decimal(str(record['value']))
            if not value.is_finite() or not math.isfinite(float(value)):
                raise ValueError('finite observations required')
            if not isinstance(record.get('unit'),str) or not record['unit']:
                raise ValueError('observation unit required')
            units.add(record['unit'])
            points.append((stamp,float(value)))
            if not record.get('sources'):
                raise ValueError('each point must retain source references')
            for source in record['sources']:
                sources.add(' | '.join(str(source.get(k,'')) for k in ('source_id','url','locator')))
        if len(units)>1:
            raise ValueError('mixed units require separate panels')
        panels.append((key,points,next(iter(units)) if units else 'no observations'))
    output = Path(output_path)
    if output.exists():
        raise FileExistsError('retain earlier plot, choose a new output filename')
    wrapped = [p for line in sorted(sources) for p in textwrap.wrap(line, width=110)]
    with tempfile.TemporaryDirectory(prefix='market-matplotlib-') as cache:
        os.environ.setdefault('MPLCONFIGDIR',cache)
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib import font_manager
        names = {f.name for f in font_manager.fontManager.ttflist}
        font = next((n for n in ('PingFang SC','Arial Unicode MS','Noto Sans CJK SC','Heiti TC') if n in names),'DejaVu Sans')
        plt.rcParams.update({'font.family':font,'text.usetex':False,'text.parse_math':False,'axes.unicode_minus':False})
        footer = .35 + .16*len(wrapped)
        height = 1.85 + 2.6*len(panels) + footer
        fig, axes = plt.subplots(len(panels),1,figsize=(12,height),squeeze=False)
        fig.subplots_adjust(left=.12,right=.97,top=1-.9/height,bottom=(footer+.7)/height,hspace=.7)
        mode = data.get('data_mode','source observations; provenance not re-audited by renderer')
        fig.suptitle(f"{month} | {data['status']} | {mode}",y=1-.12/height,fontsize=14)
        fig.text(.03,1-.48/height,'Cutoff: '+str(data.get('as_of') or 'no observations')+' | Reported dates only; no zero filling or interpolation.',fontsize=9)
        for ax,(key,points,unit) in zip(axes[:,0],panels):
            ax.set_title(LABELS.get(key,key),loc='left',fontsize=11)
            ax.set_ylabel(unit)
            has_time=any(stamp.tzinfo is not None for stamp,_ in points)
            ax.set_xlabel('Observed timestamp (UTC; original offsets in JSON)' if has_time else 'Reported exchange-local trading date')
            ax.grid(True,alpha=.22)
            ax.ticklabel_format(axis='y',style='plain',useOffset=False)
            if points:
                xs,ys=zip(*points)
                ax.plot(xs,ys,marker='o',linestyle='--',linewidth=1,color='#a65e00' if data['status']=='candidate_unreviewed' else '#176271',label=data['status'])
                ax.set_xlim(min(xs)-timedelta(days=1),max(xs)+timedelta(days=1))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=2,maxticks=8))
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M' if has_time else '%m-%d',tz=timezone.utc))
                ax.legend(loc='best',fontsize=8)
            else:
                ax.text(.5,.5,'No reported data in this month/status',transform=ax.transAxes,ha='center',va='center')
                ax.set_yticks([])
                ax.set_xlim(datetime.combine(start,datetime.min.time()),datetime(start.year,start.month,calendar.monthrange(start.year,start.month)[1],23,59))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=2,maxticks=5))
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
        fig.text(.03,(footer-.12)/height,'Sources (full references retained in input JSON):',fontsize=8)
        for i,line in enumerate(wrapped):
            fig.text(.03,(footer-.32-i*.16)/height,line,fontsize=7)
        buffer=BytesIO()
        try:
            fig.savefig(buffer,format='png',dpi=120,facecolor='white')
        finally:
            plt.close(fig)
        png=buffer.getvalue()
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open('xb') as f:
            f.write(png)
        return {'output':str(output),'input_sha256':hashlib.sha256(raw).hexdigest(),'output_sha256':hashlib.sha256(png).hexdigest(),
                'dependency':'matplotlib','version':matplotlib.__version__,'panels':len(panels),'observations':sum(len(p) for _,p,_ in panels),
                'status':data['status'],'official_reference':'https://matplotlib.org/stable/gallery/lines_bars_and_markers/simple_plot.html'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('input',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();print(json.dumps(plot_market(args.input,args.output),ensure_ascii=False))
