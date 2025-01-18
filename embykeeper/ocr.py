from enum import IntEnum
from typing import Optional, Union
from io import BytesIO
from multiprocessing import Process, Queue
import asyncio
import time
import uuid

from .data import get_datas


class CharRange(IntEnum):
    NUMBER = 0
    LLETTER = 1
    ULETTER = 2
    LLETTER_ULETTER = 3
    NUMBER_LLETTER = 4
    NUMBER_ULETTER = 5
    NUMBER_LLETTER_ULETTER = 6
    NOT_NUMBER_LLETTER_ULETTER = 7


class OCRService:
    # 添加类变量用于进程池
    _pool = {}
    _pool_lock = asyncio.Lock()

    @classmethod
    async def get(
        cls,
        ocr_name: str = None,
        char_range: Optional[Union[CharRange, str]] = None,
        basedir: str = None,
        proxy: dict = None,
    ):
        # 创建用于标识唯一实例的键
        key = (ocr_name, char_range)
        async with cls._pool_lock:
            # 检查池中是否存在相同配置的实例
            if key in cls._pool:
                return cls._pool[key]
            instance = cls(ocr_name, char_range, basedir, proxy)
            cls._pool[key] = instance
            return instance

    def __init__(
        self,
        ocr_name: str = None,
        char_range: Optional[Union[CharRange, str]] = None,
        basedir: str = None,
        proxy: dict = None,
    ) -> None:
        self.ocr_name = ocr_name
        self.char_range = char_range
        self.basedir = basedir
        self.proxy = proxy

        self._process = None
        self._queue_in = None  # 发送图片数据的队列
        self._queue_out = None  # 接收识别结果的队列
        self._subscribers = 0
        self._last_active = time.time()
        self._stop_event = None
        self._monitor_task = None
        self._pending_requests = {}  # 存储待处理的请求

    async def start(self):
        """启动OCR进程"""
        if self._process and self._process.is_alive():
            return

        self._queue_in = Queue()
        self._queue_out = Queue()
        self._stop_event = asyncio.Event()

        self._process = Process(
            target=self._process_main,
            args=(
                self._queue_in,
                self._queue_out,
                self.ocr_name,
                self.char_range,
                self.basedir,
                self.proxy,
            ),
            daemon=True,
        )
        self._process.start()

        # 启动监控任务
        self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self, force: bool = False):
        """停止OCR进程"""
        if force:
            await self.force_stop()
        else:
            self._subscribers = 0  # 这将触发监控任务在空闲超时后关闭进程

    async def force_stop(self):
        """强制停止OCR进程"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # 处理所有未完成的请求
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(Exception("OCR进程已停止"))
        self._pending_requests.clear()

        if self._process and self._process.is_alive():
            self._queue_in.put(("stop", None))
            self._process.join(timeout=1)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join()

        self._process = None
        self._queue_in = None
        self._queue_out = None
        self._stop_event = None

    async def run(self, image_data: BytesIO, timeout: int = 30) -> str:
        """发送图片到OCR进程并等待结果"""
        if not self._process or not self._process.is_alive():
            await self.start()

        # 生成唯一请求ID
        request_id = str(uuid.uuid4())
        future = asyncio.Future()
        self._pending_requests[request_id] = future

        try:
            self._last_active = time.time()
            self._queue_in.put(("process", (request_id, image_data.getvalue())))
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        finally:
            self._pending_requests.pop(request_id, None)

    def subscribe(self):
        """增加使用者计数"""
        self._subscribers += 1
        self._last_active = time.time()

    def unsubscribe(self):
        """减少使用者计数"""
        self._subscribers = max(0, self._subscribers - 1)
        self._last_active = time.time()

    async def _monitor(self):
        """监控进程状态和空闲时间，同时处理返回结果"""
        while True:
            try:
                # 检查进程状态和空闲超时
                if self._subscribers == 0 and time.time() - self._last_active > 300:
                    await self.force_stop()
                    break

                if self._process and not self._process.is_alive():
                    await self.force_stop()
                    break

                # 非阻塞方式检查结果队列
                try:
                    status, (request_id, result) = await asyncio.get_event_loop().run_in_executor(
                        None, self._queue_out.get_nowait
                    )

                    # 找到对应的future并设置结果
                    if request_id in self._pending_requests:
                        future = self._pending_requests[request_id]
                        if status == "error":
                            future.set_exception(Exception(result))
                        else:
                            future.set_result(result)
                except (asyncio.CancelledError, Exception):
                    pass

                await asyncio.sleep(0.1)  # 短暂休眠避免CPU占用过高

            except asyncio.CancelledError:
                break

    @staticmethod
    def _process_main(*args, **kw):
        return asyncio.run(OCRService._async_process_main(*args, **kw))

    @staticmethod
    async def _async_process_main(
        queue_in: Queue,
        queue_out: Queue,
        ocr_name: str,
        char_range: Optional[Union[CharRange, str]],
        basedir: str,
        proxy: dict,
    ):
        model = None
        use_probability = False

        try:
            from ddddocr import DdddOcr
            from onnxruntime.capi.onnxruntime_pybind11_state import InvalidProtobuf
            from PIL import Image

            # 加载模型
            if not ocr_name:
                model = DdddOcr(beta=True, show_ad=False)
                if char_range:
                    use_probability = True
                    model.set_ranges(char_range)
            else:
                data = []
                files = (f"{ocr_name}.onnx", f"{ocr_name}.json")
                async for p in get_datas(basedir, files, proxy=proxy, caller="OCR"):
                    if p is None:
                        queue_out.put(("error", "无法下载所需文件"))
                        return
                    data.append(p)
                try:
                    model = DdddOcr(show_ad=False, import_onnx_path=str(data[0]), charsets_path=str(data[1]))
                except InvalidProtobuf:
                    queue_out.put(("error", "文件下载不完全"))
                    return

            # 处理请求循环
            while True:
                try:
                    cmd, data = queue_in.get()
                except KeyboardInterrupt:
                    break
                if cmd == "stop":
                    break

                request_id, image_data = data
                try:
                    image = Image.open(BytesIO(image_data))
                    if use_probability:
                        ocr_result = model.classification(image, probability=True)
                        ocr_text = ""
                        for i in ocr_result["probability"]:
                            ocr_text += ocr_result["charsets"][i.index(max(i))]
                    else:
                        ocr_text = model.classification(image)
                    queue_out.put(("success", (request_id, ocr_text)))
                except Exception as e:
                    queue_out.put(("error", (request_id, str(e))))

        finally:
            if model:
                del model

    def __enter__(self):
        """上下文管理器入口"""
        self.subscribe()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.unsubscribe()
        return False  # 返回False允许异常正常传播