# Contains code from https://github.com/duckdblabs/db-benchmark/blob/master/pandas/join-pandas.py

import bigframes.pandas as bpd

print("Join benchmark 4: medium inner on factor")

x = bpd.read_gbq("bigframes-dev-perf.dbbenchmark.J1_1e9_NA_0_0")
medium = bpd.read_gbq("bigframes-dev-perf.dbbenchmark.J1_1e9_1e6_0_0")

ans = x.merge(medium, on="id5")
print(ans.shape)

chk = [ans["v1"].sum(), ans["v2"].sum()]
print(chk)

bpd.reset_session()
