"""学习通（超星）待办提醒 - 核心包。

模块划分：
    config   配置加载与校验
    models   数据模型（课程 / 任务）
    parsers  HTML / JSON 解析（纯函数，可单测）
    client   登录与网络请求
    state    去重状态持久化
    notify   多通道消息推送
    runner   轮询主循环与提醒决策
    cli      命令行入口
"""

__version__ = "1.0.0"
