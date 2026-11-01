"""Convert Binance 1s kline zips into one npz of (second, close, volume, taker_buy_volume)."""
import sys, zipfile, glob, os
import numpy as np
src, out = sys.argv[1], sys.argv[2]
secs, close, vol, tbv = [], [], [], []
for path in sorted(glob.glob(os.path.join(src, "*.zip"))):
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        data = np.genfromtxt(z.open(name), delimiter=",", usecols=(0, 4, 5, 9))
    t = data[:, 0]
    # Binance switched spot kline timestamps to microseconds in 2025.
    t = np.where(t > 1e14, t / 1e6, np.where(t > 1e11, t / 1e3, t)).astype(np.int64)
    secs.append(t); close.append(data[:, 1]); vol.append(data[:, 2]); tbv.append(data[:, 3])
    print(os.path.basename(path), len(t), t[0], t[-1], flush=True)
secs = np.concatenate(secs); order = np.argsort(secs)
np.savez_compressed(out, sec=secs[order], close=np.concatenate(close)[order], vol=np.concatenate(vol)[order], tbv=np.concatenate(tbv)[order])
s = secs[order]; print("total", len(s), "gaps>1s:", int((np.diff(s) > 1).sum()), "max gap", int(np.diff(s).max()))
