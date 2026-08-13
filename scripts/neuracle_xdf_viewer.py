#!/usr/bin/env python3
"""Build an interactive playback/QC HTML for a Neuracle W64 XDF recording.

The XDF is never modified. The orange overlay is a diagnostic processing view:
60 Hz notch -> 0.5-45 Hz band-pass -> ECG/EOG nuisance regression ->
common-average reference. It is not a claim that artifacts are perfectly removed.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any

import numpy as np

SCALP = (
    "Fpz","Fp1","Fp2","AF3","AF4","AF7","AF8","Fz","F1","F2","F3","F4","F5","F6","F7","F8",
    "FCz","FC1","FC2","FC3","FC4","FC5","FC6","FT7","FT8","Cz","C1","C2","C3","C4","C5","C6","T7","T8",
    "CP1","CP2","CP3","CP4","CP5","CP6","TP7","TP8","Pz","P3","P4","P5","P6","P7","P8",
    "POz","PO3","PO4","PO5","PO6","PO7","PO8","Oz","O1","O2",
)
AUX = ("ECG","HEOR","HEOL","VEOU","VEOL")
TRIGGER = "TRIGGER_STATUS"
CANON64 = (*SCALP, *AUX)
CANON65 = (*CANON64, TRIGGER)
BANDS = {"delta":(1.,4.),"theta":(4.,8.),"alpha":(8.,13.),"beta":(13.,30.),"gamma":(30.,45.)}
REGIONS = {
    "Frontal":("Fpz","Fp1","Fp2","AF3","AF4","AF7","AF8","Fz","F1","F2","F3","F4","F5","F6","F7","F8"),
    "Fronto-central":("FCz","FC1","FC2","FC3","FC4","FC5","FC6"),
    "Central":("Cz","C1","C2","C3","C4","C5","C6"),
    "Temporal":("FT7","FT8","T7","T8"),
    "Centro-parietal":("CP1","CP2","CP3","CP4","CP5","CP6"),
    "Temporo-parietal":("TP7","TP8","P7","P8"),
    "Parietal":("Pz","P3","P4","P5","P6"),
    "Occipital":("POz","PO3","PO4","PO5","PO6","PO7","PO8","Oz","O1","O2"),
}


def first(x: Any, default: Any = None) -> Any:
    while isinstance(x, list) and x:
        x = x[0]
    return default if x is None else x


def norm(x: str) -> str:
    return "".join(c for c in str(x).upper() if c.isalnum())


def select_stream(streams: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = []
    for s in streams:
        a = np.asarray(s.get("time_series"))
        if a.ndim != 2 or a.shape[0] < 2 or not np.issubdtype(a.dtype, np.number):
            continue
        typ = str(first(s.get("info",{}).get("type"),"")).lower()
        candidates.append((typ == "eeg", a.shape[1], s))
    if not candidates:
        raise RuntimeError("No numeric multichannel stream found in XDF")
    candidates.sort(key=lambda x:(x[0],x[1]), reverse=True)
    return candidates[0][2]


def channel_meta(stream: dict[str, Any], n: int) -> tuple[list[str],list[str],list[str]]:
    try:
        rows = stream["info"]["desc"][0]["channels"][0]["channel"]
    except (KeyError,IndexError,TypeError):
        rows = []
    labels=[]; types=[]; units=[]
    for i in range(n):
        r = rows[i] if i < len(rows) and isinstance(rows[i],dict) else {}
        labels.append(str(first(r.get("label"),f"Ch{i+1:02d}")))
        types.append(str(first(r.get("type"),"")))
        units.append(str(first(r.get("unit"),"")))
    return labels,types,units


def generic(labels: list[str]) -> bool:
    hit=0
    for x in labels:
        z=norm(x)
        hit += int((z.startswith("CH") and z[2:].isdigit()) or (z.startswith("CHANNEL") and z[7:].isdigit()))
    return hit >= max(1,int(.8*len(labels)))


def canonicalize(labels: list[str]) -> tuple[list[str],str]:
    if generic(labels) and len(labels)==65: return list(CANON65),"canonical Neuracle W64 fallback"
    if generic(labels) and len(labels)==64: return list(CANON64),"canonical Neuracle W64 fallback"
    return labels,"XDF channel labels"


def indices(labels: list[str], wanted: tuple[str,...]) -> list[int]:
    w={norm(x) for x in wanted}
    return [i for i,x in enumerate(labels) if norm(x) in w]


def clean(data: np.ndarray) -> tuple[np.ndarray,float]:
    a=np.asarray(data,dtype=np.float64)
    ok=np.isfinite(a); frac=1.-float(ok.mean())
    if ok.all(): return a,frac
    for c in range(a.shape[1]):
        good=np.isfinite(a[:,c]); fill=float(np.median(a[good,c])) if good.any() else 0.
        a[~good,c]=fill
    return a,frac


def rates(stream: dict[str, Any], ts: np.ndarray) -> tuple[float,float]:
    nominal=float(first(stream.get("info",{}).get("nominal_srate"),0.) or 0.)
    d=np.diff(ts); d=d[np.isfinite(d)&(d>0)]
    effective=float(1./np.median(d)) if d.size else nominal
    return (nominal or effective),effective


def unit_mode(units: list[str], scalp_idx: list[int]) -> tuple[str,str,float]:
    u=[units[i].strip().lower().replace("μ","µ") for i in scalp_idx if i<len(units) and units[i].strip()]
    if u and all(x in {"µv","uv","microvolt","microvolts"} for x in u): return "µV","uv",1.
    if u and all(x in {"v","volt","volts"} for x in u): return "V","v",1e6
    if u and all(x in {"mv","millivolt","millivolts"} for x in u): return "mV","mv",1e3
    return (u[0] if u and len(set(u))==1 else "native"),"native",1.


def processed(data: np.ndarray, fs: float, scalp_idx: list[int], aux_idx: list[int]) -> tuple[np.ndarray,dict[str,Any]]:
    from scipy import signal
    if not scalp_idx: raise RuntimeError("No scalp EEG channels identified")
    eeg=np.asarray(data[:,scalp_idx],dtype=np.float64)
    hi=min(45.,fs*.45)
    if hi <= .5: raise RuntimeError(f"Sampling rate {fs:g} Hz is too low for EEG processing")
    if fs>125:
        b,a=signal.iirnotch(60.,Q=30.,fs=fs); eeg=signal.filtfilt(b,a,eeg,axis=0)
    sos=signal.butter(4,[.5,hi],btype="bandpass",fs=fs,output="sos")
    eeg=signal.sosfiltfilt(sos,eeg,axis=0)
    used=[]; notes=[]
    if aux_idx:
        n=np.asarray(data[:,aux_idx],dtype=np.float64)
        if fs>125:
            b,a=signal.iirnotch(60.,Q=30.,fs=fs); n=signal.filtfilt(b,a,n,axis=0)
        n=signal.sosfiltfilt(sos,n,axis=0)
        med=np.median(n,axis=0); scale=1.4826*np.median(np.abs(n-med),axis=0)
        keep=np.isfinite(scale)&(scale>np.finfo(float).eps)
        if keep.any():
            n=(n[:,keep]-med[keep])/scale[keep]; used=[aux_idx[i] for i in np.flatnonzero(keep)]
            m=min(n.shape[0],120_000)
            pick=np.linspace(0,n.shape[0]-1,m,dtype=int) if n.shape[0]>m else np.arange(n.shape[0])
            design=np.column_stack([np.ones(pick.size),n[pick]])
            beta,*_=np.linalg.lstsq(design,eeg[pick],rcond=1e-7)
            eeg=eeg-n@beta[1:]
            notes.append(f"regressed {len(used)} non-flat ECG/EOG leads using {pick.size:,} fit samples")
        else: notes.append("ECG/EOG leads found but flat; nuisance regression skipped")
    else: notes.append("no ECG/EOG leads identified; nuisance regression skipped")
    if eeg.shape[1]>1: eeg=eeg-eeg.mean(axis=1,keepdims=True)
    return eeg.astype(np.float32),{"chain":["60 Hz notch","0.5-45 Hz zero-phase band-pass","ECG/EOG linear nuisance regression","common-average reference"],"aux_indices_used":used,"notes":notes}


def robust_sd(a: np.ndarray) -> np.ndarray:
    m=np.median(a,axis=0); return 1.4826*np.median(np.abs(a-m),axis=0)


def qc(data: np.ndarray, labels: list[str], scalp_idx: list[int], aux_idx: list[int], trig_idx: list[int], fs: float) -> tuple[list[dict[str,Any]],dict[str,int],float]:
    from scipy import signal
    x=data-np.median(data,axis=0,keepdims=True); rs=robust_sd(x)
    p=np.percentile(x,[.5,99.5],axis=0); pp=p[1]-p[0]
    dx=np.diff(x,axis=0); flat=np.mean(np.abs(dx)<=np.maximum(rs,1e-15)[None,:]*1e-6,axis=0) if dx.size else np.ones(x.shape[1])
    line=np.full(x.shape[1],np.nan)
    if fs>130 and x.shape[0]>=int(2*fs):
        sub=x[:min(x.shape[0],int(120*fs))]; f,psd=signal.welch(sub,fs=fs,nperseg=min(int(2*fs),sub.shape[0]),axis=0)
        base=(f>=1)&(f<=45); mains=(f>=58)&(f<=62)
        if base.any() and mains.any():
            line=np.trapezoid(psd[mains],f[mains],axis=0)/np.maximum(np.trapezoid(psd[base],f[base],axis=0),1e-30)
    scalp_rs=rs[scalp_idx] if scalp_idx else rs; good=scalp_rs[np.isfinite(scalp_rs)&(scalp_rs>0)]
    typical=float(np.median(good)) if good.size else 1.; rows=[]; counts={"good":0,"suspect":0,"bad":0}
    for i,name in enumerate(labels):
        role="EEG" if i in scalp_idx else "AUX" if i in aux_idx else "TRIGGER" if i in trig_idx else "OTHER"
        status="info"; issues=[]; ratio=float(rs[i]/max(typical,1e-30))
        if role=="EEG":
            status="good"
            if not np.isfinite(rs[i]) or flat[i]>.98 or ratio<.05: status="bad";issues.append("flat/near-flat")
            elif ratio>12: status="bad";issues.append("extreme amplitude")
            else:
                if ratio<.2: status="suspect";issues.append("low amplitude")
                if ratio>5: status="suspect";issues.append("high amplitude")
                if np.isfinite(line[i]) and line[i]>.4: status="suspect";issues.append("strong 60 Hz component")
            counts[status]+=1
        rows.append({"index":i,"channel":name,"role":role,"status":status,"robust_std":float(rs[i]),"p2p_99pct":float(pp[i]),"flat_fraction":float(flat[i]),"line60_ratio":None if not np.isfinite(line[i]) else float(line[i]),"issues":issues})
    return rows,counts,typical


def powers(eeg: np.ndarray, fs: float, scalp_names: list[str]) -> dict[str,Any]:
    from scipy import signal
    win=max(8,int(round(2*fs))); step=max(1,int(round(.5*fs)))
    if eeg.shape[0]<win: return {"times":[],"bands":{b:[] for b in BANDS},"regions":{},"regionChannels":{}}
    starts=np.arange(0,eeg.shape[0]-win+1,step); w=signal.windows.hann(win,sym=False).astype(np.float32)
    freqs=np.fft.rfftfreq(win,1/fs); total=(freqs>=1)&(freqs<=45); band_masks={b:(freqs>=lo)&(freqs<hi) for b,(lo,hi) in BANDS.items()}
    name_to_i={n:i for i,n in enumerate(scalp_names)}; region_idx={r:[name_to_i[n] for n in names if n in name_to_i] for r,names in REGIONS.items()}; region_idx={r:v for r,v in region_idx.items() if v}
    out={b:[] for b in BANDS}; reg={r:{b:[] for b in BANDS} for r in region_idx}; times=[]
    for s in starts:
        z=np.fft.rfft(eeg[s:s+win]*w[:,None],axis=0); ps=(z.real*z.real+z.imag*z.imag).astype(np.float64)
        den=np.maximum(ps[total].sum(axis=0),1e-30); rel={b:100.*ps[m].sum(axis=0)/den for b,m in band_masks.items()}
        for b in BANDS: out[b].append(float(np.mean(rel[b])))
        for r,idx in region_idx.items():
            for b in BANDS: reg[r][b].append(float(np.mean(rel[b][idx])))
        times.append(float((s+win/2)/fs))
    return {"times":times,"bands":out,"regions":reg,"regionChannels":{r:[scalp_names[i] for i in idx] for r,idx in region_idx.items()}}


def quantize(a: np.ndarray) -> tuple[str,float,float]:
    finite=np.abs(a[np.isfinite(a)]); clip=float(np.percentile(finite,99.95)) if finite.size else 1.; clip=max(clip,1e-30); scale=clip/30000.
    q=np.clip(np.rint(a/scale),-32767,32767).astype("<i2"); clipped=float(np.mean(np.abs(a)>clip))
    return base64.b64encode(q.tobytes()).decode(),scale,clipped


def f32b64(a: np.ndarray) -> str:
    return base64.b64encode(np.asarray(a,dtype="<f4").tobytes()).decode()


def build(inp: Path, out: Path, summary_path: Path, template_path: Path) -> dict[str,Any]:
    import pyxdf
    streams,_=pyxdf.load_xdf(str(inp)); stream=select_stream(streams)
    raw=np.asarray(stream["time_series"]); ts=np.asarray(stream["time_stamps"],dtype=np.float64)
    if raw.ndim!=2 or ts.size!=raw.shape[0]: raise RuntimeError("Selected XDF stream has inconsistent data/timestamps")
    order=np.argsort(ts,kind="stable"); ts=ts[order]; raw=raw[order]; raw,nonfinite=clean(raw)
    labels0,types,units=channel_meta(stream,raw.shape[1]); labels,label_source=canonicalize(labels0)
    scalp_idx=indices(labels,SCALP); aux_idx=indices(labels,AUX); trig_idx=indices(labels,(TRIGGER,))
    fs_nom,fs_eff=rates(stream,ts); fs=fs_nom or fs_eff
    corrected,proc=processed(raw,fs,scalp_idx,aux_idx)
    qrows,qcounts,typical=qc(raw,labels,scalp_idx,aux_idx,trig_idx,fs)
    scalp_names=[labels[i] for i in scalp_idx]; power=powers(corrected,fs,scalp_names)
    storage_unit,default_mode,known_mult=unit_mode(units,scalp_idx)

    # Browser trace payload: about 100 samples/s, actual timestamps retained.
    stride=max(1,int(round(fs/100.))); pick=np.arange(0,raw.shape[0],stride); rel=(ts-ts[0])[pick]
    raw_plot=raw[pick].copy(); raw_plot-=np.median(raw_plot,axis=0,keepdims=True)
    plot_idx=[i for i in range(raw.shape[1]) if i not in trig_idx]
    raw_plot=raw_plot[:,plot_idx]; raw_names=[labels[i] for i in plot_idx]
    cor_plot=corrected[pick]
    rb,rs,rc=quantize(raw_plot); cb,cs,cc=quantize(cor_plot)
    d=np.diff(ts); positive=d[np.isfinite(d)&(d>0)]; gap_cut=max(.1,5.*float(np.median(positive)) if positive.size else .1); gaps=int(np.sum(d>gap_cut)); largest=float(np.max(d)) if d.size else 0.
    duration=float(ts[-1]-ts[0]) if ts.size else 0.
    unit_warning=None if default_mode!="native" else "XDF channel units are missing or mixed. Trace geometry is valid, but absolute amplitudes remain in native stored units unless you choose an explicit unit assumption."
    auto=max(float(typical),1e-12)
    payload={
        "meta":{"input":str(inp),"stream":str(first(stream.get("info",{}).get("name"),"")),"streamType":str(first(stream.get("info",{}).get("type"),"")),"samples":int(raw.shape[0]),"channels":int(raw.shape[1]),"duration":duration,"sampleRate":float(fs_nom),"effectiveRate":float(fs_eff),"channelLabelSource":label_source,"scalpCount":len(scalp_idx),"auxFound":[labels[i] for i in aux_idx],"triggerFound":[labels[i] for i in trig_idx],"unit":storage_unit,"defaultUnitMode":default_mode,"knownUnitMultiplierToUv":known_mult,"unitWarning":unit_warning,"nonfiniteFraction":nonfinite,"gaps":gaps,"gapThreshold":gap_cut,"largestGap":largest,"qcCounts":qcounts,"autoScale":auto,"processing":proc},
        "trace":{"timesB64":f32b64(rel),"rawB64":rb,"rawScale":rs,"rawChannels":raw_names,"rawChannelCount":len(raw_names),"correctedB64":cb,"correctedScale":cs,"correctedChannels":scalp_names,"correctedChannelCount":len(scalp_names),"stride":stride,"clippedFractionRaw":rc,"clippedFractionCorrected":cc},
        "power":power,"qc":qrows,
    }
    template=template_path.read_text(encoding="utf-8")
    if "__PAYLOAD__" not in template: raise RuntimeError("HTML template is missing __PAYLOAD__ placeholder")
    out.write_text(template.replace("__PAYLOAD__",json.dumps(payload,separators=(",",":"))),encoding="utf-8")
    summary={"input":str(inp),"output":str(out),"stream":{"name":payload["meta"]["stream"],"type":payload["meta"]["streamType"],"samples":raw.shape[0],"channels":raw.shape[1],"duration_seconds":duration,"nominal_rate_hz":fs_nom,"effective_rate_hz":fs_eff},"channel_label_source":label_source,"scalp_channels":scalp_names,"aux_channels_found":payload["meta"]["auxFound"],"trigger_channels_found":payload["meta"]["triggerFound"],"storage_unit":storage_unit,"nonfinite_fraction":nonfinite,"timestamp_gap_count":gaps,"largest_timestamp_gap_seconds":largest,"processing":proc,"qc_counts":qcounts,"qc_bad_channels":[r["channel"] for r in qrows if r["status"]=="bad"],"qc_suspect_channels":[r["channel"] for r in qrows if r["status"]=="suspect"],"region_channels":power["regionChannels"],"display":{"stride":stride,"samples":len(pick),"raw_clipped_fraction":rc,"corrected_clipped_fraction":cc}}
    summary_path.write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print(json.dumps(summary,indent=2)); return summary


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input",default="recording.xdf"); p.add_argument("--output",default="recording_view.html"); p.add_argument("--summary",default="recording_qc.json"); p.add_argument("--template",default="scripts/neuracle_xdf_viewer_template.html")
    a=p.parse_args(); build(Path(a.input),Path(a.output),Path(a.summary),Path(a.template)); return 0


if __name__=="__main__": raise SystemExit(main())
