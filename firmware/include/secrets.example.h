#pragma once

// 模板文件：复制成同目录下的 secrets.h 再填真实值。
//   Windows:  copy secrets.example.h secrets.h
//   Linux/Mac: cp secrets.example.h secrets.h
//
// secrets.h 已被 .gitignore 排除，永远不会进仓库；这个模板里没有任何真实凭据。
// 仓库里所有代码只引用 secrets.h，所以缺了它编译会失败——这是故意的，
// 强制每个人各自配置，而不是共用一个能提交上来的凭据文件。

#define WIFI_SSID "你的WiFi名称"
#define WIFI_PASSWORD "你的WiFi密码"

// 服务端地址。板子要主动连它，所以必须是服务端所在机器的「局域网 IP」，
// 不能填 127.0.0.1（那指向板子自己）。在跑 uvicorn 的机器上查：
//   Windows: ipconfig        Linux/Mac: ip addr 或 ifconfig
// 常见的内网段是 192.168.x.x / 10.x.x.x / 172.16~31.x.x。
#define SERVER_URL "http://192.168.1.100:8000"

// 与服务端 .env 里的 INGEST_TOKEN 保持一致。
// 服务端没设该项时留空字符串即可（服务端不校验）。
#define INGEST_TOKEN ""
