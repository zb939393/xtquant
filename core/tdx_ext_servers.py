# -*- coding: utf-8 -*-
"""扩展行情主站池（端口 7721，pytdx.exhq.TdxExHq_API）。

单一数据源：core/ak_service.py（登记 / 统计 / 延迟优选）与 core/futures_service.py /
core/option_exquote_service.py（消费）共用，避免多份副本漂移。

来源（均取自各自券商客户端的 connect.cfg [DSHOST] 段，并逐台 TdxExHq_API 实测）：
  - 长城证券（D:/zd_cczq 烽火版）：4 台可用；凤岗移动x 183.239.167.196 /
    北京云多线 109.244.14.19 两台 TCP 不可达，未计入。
  - 国元证券（D:/国元领航/[DSHOST] 6 个 IPv4）：5 台可用；
    中国电信2 60.173.222.50 连上但盘口为空（空镜像），未计入。
  - 国信证券（D:/zd_gxzq [DSHOST] 3 个 IPv4）：3 台全部可用。

实测口径（2026-09-10）：每台均验证「期指 IFL9(市场47) 盘口 + 期权 沪10010973(市场8) /
深90007105(市场9) 盘口」三通道均返回真实价（IFL9=4480.20；期权 0.0768 / 0.0839），
仅 get_instrument_count() 有值不足以判定可用（曾见 count 有值但盘口全空的空镜像）。

重要：7721 为通达信扩展行情端口，须用 pytdx.exhq.TdxExHq_API；用标准 TdxHq_API 连会报
`head_buf is not 0x10`（协议不匹配）。本模块刻意不 import 任何重依赖（如 akshare），
以便 futures_service / option_exquote_service 可在不触发 ak_service 全量导入的前提下引用本池。
"""
_TDX_EXT_SERVERS = [
    ("长城扩展行情深圳电信", "219.133.95.102", 7721),
    ("长城扩展行情深圳联通", "210.21.198.233", 7721),
    ("长城扩展行情苏州电信", "58.210.106.10", 7721),
    ("长城扩展行情苏州移动", "223.112.100.140", 7721),
    ("国元扩展行情合肥电信1", "61.191.48.15", 7721),
    ("国元扩展行情合肥联通1", "220.248.233.5", 7721),
    ("国元扩展行情合肥联通2", "218.106.80.15", 7721),
    ("国元扩展行情合肥移动1", "120.210.144.2", 7721),
    ("国元扩展行情合肥移动2", "221.130.121.45", 7721),
    ("国信扩展行情腾讯云华东信创", "109.244.35.23", 7721),
    ("国信扩展行情阿里云华南", "120.79.210.76", 7721),
    ("国信扩展行情阿里云华东", "139.224.201.59", 7721),
]

# 兼容旧调用点：只需要 (host, port) 的地方（连接优选 / 状态统计）
_PYTDX_EXT_SERVERS = [(ip, port) for (_n, ip, port) in _TDX_EXT_SERVERS]

# tdx_exhq.connect_exhq / get_option_codes 需要 (host, port, name) 三元组顺序
_TDX_EXT_SERVERS_FOR_TDX_EXHQ = [(ip, port, name) for (name, ip, port) in _TDX_EXT_SERVERS]
