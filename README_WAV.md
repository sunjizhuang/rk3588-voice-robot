[文件夹监控 Python 进程]
        |
        | 发现写完的 wav，按顺序入队
        v
[播放队列]
        |
        | 逐个读取 wav -> 解码为 PCM
        v
[常驻 aplay 进程的 stdin]  <-- 只启动一次
        |
        v
     扬声器

该文件需要调整


使用方式：
# 使用默认目录 /tmp/wav_queue
python3 wav_player.py

# 指定目录
python3 wav_player.py /home/user/wavs


检查 ALSA 音量:alsamixer


关键难点说明
你要求“长期保持一个 aplay 进程监听”，但 aplay 本身不具备“监听文件夹/等待任务”的能力。
aplay 是一个一次性命令：给它一个 WAV 文件或标准输入流，它播放完就退出。
所以真正能实现“长期常驻、避免反复启动 aplay”的方案只有一条路：

让一个长期运行的 aplay 从标准输入（stdin）持续读取 PCM 数据播放，Python 负责把 WAV 文件转成 PCM 流喂给它。

也就是：

bash
aplay -t raw -f S16_LE -r 44100 -c 2 -
这个 aplay 进程只启动一次，之后一直活着，Python 把每个 WAV 解码后的 PCM 数据依次写进它的 stdin，就能实现“播完一个接着播下一个”，完全避免反复 fork/exec aplay 的开销
