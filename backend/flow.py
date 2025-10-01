# backend/flow.py
def big_prints(symbol, min_sz=30, window_s=5, radius_ticks=2, around=None):
    # filter trades by time + optional price band around 'around'
    # group by side; sum size; return clusters above thresholds
    ...
