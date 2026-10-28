# 第 8 周样例对照表：正常 / 低分 / 错误 / 未知

对应当堂验证与提交要求："正常、低分、错误和未知样例对照"。

## 四条铁律先说清

1. **分数未校准**：下表所有 score 都是模型置信度（或回放占位的固定值），
   不是现实正确率；任何界面/答复都固定带"未校准，不代表现实正确率"。
2. **来源如实标注**：`source=model` 才是真实模型推理；`source=replay` 是
   回放/规则占位，只演示界面与流程，**不计入真实推理证据**。
3. 场景类别（scene）只是粗分类，不转换为精确位置或人身安全判断。
4. 样例来源按列标注：**现场运行**（`python tools/demo_week8.py` 真实 HTTP 输出）
   与 **自动化注入**（`pytest test_vision.py`，模型输出经测试替身注入，
   用于覆盖本环境没有真实模型服务时无法现场复现的低分/拒识/错误三态）。

## 对照表

| 类别 | 触发方式 | 来源/触发 | 服务端记录（inference 行） | 界面与答复原文（节选） | 样例出处 |
|---|---|---|---|---|---|
| 正常 | “拍张照”→ 分析 | replay 回放（未配置真实模型） | status=ok, label=blurred, score=0.55, source=replay, model_version=replay-rule-1 | “（回放标注…）画面识别为‘模糊（blurred）’（分数 0.55，未校准；服务端 23:07:58 收到图像）。分数是模型的置信度，不代表现实正确率。” | 现场运行 `demo_week8.py` A2 |
| 低分 | 分析时 score 低于 `VISION_LOW_SCORE`(0.45) | 自动化注入（回放 score=0.30） | status=ok, label=usable, score=0.30 | “画面识别为‘可用（usable）’，但分数偏低（0.30，未校准，低于 0.45）——不太确定，建议重拍一张（说‘重拍’）。分数是模型置信度，不代表现实正确率。” | 自动化注入 `test_vision.py::test_low_score_feedback_suggests_recapture` |
| 错误（模型误判 → 人工纠正） | 模型判“遮挡”，同学人工核对纠正为“可用” | 现场运行（回放规则 sha256%7==0 → occluded）+ 人工纠正 | inference label=occluded；“纠正后”详情 effective=usable（corrections 表留痕） | “（回放标注…）（已按你的纠正显示）画面识别为‘可用（usable）’…”；详情页 corrections=1 条 | 现场运行 `demo_week8.py` E1/E2 |
| 未知（拒识） | 模型返回 rejected=true | 自动化注入（model 路径） | status=rejected, label=unknown, score=0.1, source=model | “画面识别**拒识**：模型无法判断画面内容…不确定看到了什么。可试着重拍一张（说‘重拍’）。” | 自动化注入 `test_vision.py::test_analyze_model_rejected` |
| 模型服务错误 | 模型服务不可达/输出非法 | 自动化注入（`_call_model` 抛 model_unavailable） | status=error, error_code=model_unavailable | “画面识别失败（model_unavailable）：模型服务不可达…”（HTTP 仍 200，失败即结果） | 自动化注入 `test_vision.py::test_analyze_model_failure_is_visible_not_lied` |

> 注：第 1、3 行为**现场运行**证据（真实 HTTP 对 127.0.0.1:8001，输出原文见
> `.artifacts/demo-week8c.log`）；第 2、4、5 行为**自动化注入**证据（测试替身
> 提供模型输出，界面与答复路径与真实代码完全相同）。接入教师真实基线后，
> 把 `VISION_BASE_URL` 指向模型服务即可让 2、4、5 变成现场证据。

## “比较高分误判与正常样例”演示口径（现场对照）

`demo_week8.py` 结尾输出对照表（两图）：

| 样例 | request_id | snapshot | 标签 | score | 采集时刻（服务端） |
|---|---|---|---|---|---|
| 正常样例 | req_0mvs9106g_c281a6 | 1 | blurred | 0.55 | 23:07:58 |
| 误判样例（纠正后） | req_0mvs9108a_99dc02 | 2 | usable(纠正) | 0.55 | 23:07:58 |

输入-结果-反馈三者对应：request_id ↔ inference 行（带 request_id 副本）↔
corrections 行（带 inference_id 与快照 id）。即使图像本体被最少保留清理，
`GET /api/v1/inferences?request_id=…` 仍可反查这条链路
（`test_vision.py::test_correspondence_survives_snapshot_prune`）。