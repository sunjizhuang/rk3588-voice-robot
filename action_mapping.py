"""动作 word_id 映射，来自 C 代码 execute_robot_action。"""

# word_id → action_str（已支持）
WORD_ID_TO_ACTION_STR = {
    1:  "stand",          # 站起来
    2:  "stand",          # 趴下（C 代码里写的就是 stand）
    3:  "forward",        # 向前走
    4:  "forward",        # 往前走
    5:  "backward",       # 向后走
    6:  "backward",       # 往后走
    7:  "strafe_left",    # 向左走
    8:  "strafe_left",    # 往左走
    9:  "strafe_right",   # 向右走
    10: "strafe_right",   # 往右走
    11: "jump",           # 向前跳
    12: "jump",           # 往前跳
    21: "turn_left_90",   # 向左转
    22: "turn_left_90",   # 往左转
    23: "turn_right_90",  # 向右转
    24: "turn_right_90",  # 往右转
    25: "turn_180",       # 往后转
    26: "turn_180",       # 向后转
    27: "stop",           # 停下
    28: "stop",           # 停止
    29: "stop",           # 停下来
    30: "stop",           # 停住
    31: "greet",          # 打招呼
    35: "twist",          # 扭身体
}

# 未支持的动作 word_id
UNSUPPORTED_WORD_IDS = {
    0,                                # 绝影绝影（旧唤醒词）
    13, 14, 15, 16, 17, 18, 19, 20,   # 上看/下看/左看/右看
    32,                               # 恭喜发财
    33,                               # 后空翻
    34,                               # 太空步
    36,                               # 翻身
    37,                               # 扭身跳
    38,                               # 地毯后空翻
    39,                               # 舞步一
    40,                               # 舞步二
    41,                               # 作揖比心
    42,                               # 扭屁股
}

