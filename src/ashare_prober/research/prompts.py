"""多视角 Prompt（T19）。

定义 ``PromptSet``：构造「持有方(bull) / 旁观方(bear)」双视角 system + user 模板。
user 模板接收**序列化后的 EventDossier**（含 evidences 的 supports/weight 双栏、cause/consequence）。
强制 JSON 输出 schema：``{"bull_view", "bear_view", "confidence", "conclusion"}``。
"""

from __future__ import annotations

from ashare_prober.attribution.models import EventDossier

# 双视角 system 指令：要求同时呈现持有方与旁观方视角，并固定 JSON schema。
_DEFAULT_SYSTEM = """你是基于「事件档案」的 A 股异动多视角研判助手。你必须同时呈现两种对立视角：
1) 持有方(bull)：尽可能找出支撑异动正面延续的理由与证据；
2) 旁观方(bear)：从风险与反证出发，审视潜在陷阱、回调与证伪可能。
只允许依据给定档案中的事实作答，严禁虚构或引入档案之外的信息。
最终必须输出一个严格 JSON 对象，字段如下：
- "bull_view"（字符串）：持有方视角的研判；
- "bear_view"（字符串）：旁观方视角的研判；
- "confidence"（0~1 浮点数）：你对整体研判结论的置信度；
- "conclusion"（字符串）：综合结论（仅作辅助研判，不荐股、不指示任何交易）。
"""

# user 模板：以 __DOSSIER__ 哨兵占位序列化后的档案 JSON
# （避免 JSON 大括号与 str.format 冲突，改用 replace 注入）。
_DEFAULT_USER = """以下是一份已结构化的「事件档案」JSON，包含前因(cause)、后果(consequence)、
证据(evidence，含 supports/weight 双栏) 等信息。

== 事件档案 ==
__DOSSIER__

== 任务 ==
请仅基于上述档案事实进行研判，不要臆造。按 system 指令输出固定 schema 的 JSON。"""


class PromptSet:
    """双视角 prompt 模板集合。"""

    def __init__(
        self,
        system_template: str = _DEFAULT_SYSTEM,
        user_template: str = _DEFAULT_USER,
    ) -> None:
        self.system_template = system_template
        self.user_template = user_template

    def build_messages(self, dossier: EventDossier) -> tuple[str, str]:
        """根据事件档案构造 (system, user) 消息。

        Args:
            dossier: 事件档案（已含 evidences 双栏与 cause/consequence）。

        Returns:
            (system_prompt, user_prompt)。
        """
        dossier_json = dossier.model_dump_json(indent=2)
        system = self.system_template
        user = self.user_template.replace("__DOSSIER__", dossier_json)
        return (system, user)
