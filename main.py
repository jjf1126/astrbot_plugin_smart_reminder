import json
import os
import re
import uuid
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger

class SmartReminderPlugin(Star):
    def __init__(self, context: Context, config: Dict):
        super().__init__(context)
        self.config = config
        self.scheduler = AsyncIOScheduler()
        
        # 修复：使用 StarTools.get_data_dir() 获取规范的数据存储目录
        # 并在其中创建一个子目录用于存放本插件的数据，避免污染根目录
        try:
            base_dir = StarTools.get_data_dir()
            # 确保转换为 Path 对象以便使用 / 操作符
            if isinstance(base_dir, str):
                base_dir = Path(base_dir)
            self.data_path = base_dir / "smart_reminder"
        except Exception as e:
            logger.warning(f"[SmartReminder] Failed to get data dir from StarTools, using default: {e}")
            self.data_path = Path("data/plugin_smart_reminder")
        
        # 确保数据目录存在
        if not self.data_path.exists():
            self.data_path.mkdir(parents=True, exist_ok=True)
            
        self.tasks_file = self.data_path / "tasks.json"
        
        # 内存中的任务缓存 {job_id: task_data}
        self.tasks: Dict[str, dict] = {}
        
        # 加载任务并启动调度器
        self._load_tasks()
        self.scheduler.start()
        
        # 启动时清理过期任务
        if self.config.get("clean_expired_on_startup", True):
            self._clean_expired_tasks()

    # ==========================
    # 核心逻辑：消息监听与分析
    # ==========================

    @filter.after_message_sent()
    async def on_message_sent(self, event: AstrMessageEvent):
        """
        在Bot发送消息后触发，分析对话上下文
        """
        try:
            # 1. 检查是否包含忽略关键词
            user_msg = event.message_str
            ignore_keywords = self.config.get("ignore_keywords", [])
            for keyword in ignore_keywords:
                if keyword in user_msg:
                    logger.debug(f"[SmartReminder] Ignored message due to keyword: {keyword}")
                    return

            # 2. 准备分析数据
            uid = event.unified_msg_origin
            # 获取最近N轮对话
            turn_count = self.config.get("context_turn_count", 10)
            
            # 从 ConversationManager 获取历史记录
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            if not curr_cid:
                return

            conversation = await self.context.conversation_manager.get_conversation(uid, curr_cid)
            if not conversation or not conversation.history:
                return
                
            history = json.loads(conversation.history)
            # 截取最近的 turn_count * 2 条消息 (user + assistant)
            recent_history = history[-(turn_count * 2):]
            
            # 3. 构建 Prompt 并调用 LLM
            await self._analyze_and_schedule(event, recent_history)

        except Exception as e:
            logger.error(f"[SmartReminder] Error in message hook: {e}")

    async def _analyze_and_schedule(self, event: AstrMessageEvent, history: List[dict]):
        """
        调用 LLM 进行分析并调度任务
        """
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        system_prompt_tmpl = self.config.get("system_prompt", "You are a helpful assistant.")
        system_prompt = system_prompt_tmpl.replace("{current_time}", current_time_str)

        # 确定使用的 Provider
        analysis_model_id = self.config.get("analysis_model_id", "")
        provider = None
        
        if analysis_model_id:
            provider = self.context.get_provider_by_id(analysis_model_id)
            if not provider:
                logger.warning(f"[SmartReminder] Configured analysis model {analysis_model_id} not found, falling back to default.")
        
        if not provider:
            provider = self.context.get_using_provider()
            
        if not provider:
            logger.error("[SmartReminder] No available LLM provider.")
            return

        try:
            # 调用 LLM
            # Prompt 应该明确要求 JSON 格式
            prompt = (
                f"当前时间是 {current_time_str}。\n"
                "请分析上述对话历史，判断用户是否希望设置提醒。\n"
                "如果是，请提取提醒时间和内容，并以严格的JSON格式输出，不要包含任何 Markdown 代码块或其他文本。\n"
                "JSON格式示例：{\"should_remind\": true, \"remind_time\": \"2023-10-01 12:00:00\", \"remind_content\": \"吃饭\"}\n"
                "如果不需要提醒，输出：{\"should_remind\": false}\n"
                "注意：remind_time 必须转换为 'YYYY-MM-DD HH:MM:SS' 格式。"
            )

            response = await provider.text_chat(
                prompt=prompt,
                contexts=[{"role": "system", "content": system_prompt}] + history,
                session_id=None
            )
            
            if not response or not response.completion_text:
                return

            # 解析 JSON
            result = self._extract_json(response.completion_text)
            if not result:
                return

            if result.get("should_remind") is True:
                remind_time_str = result.get("remind_time")
                remind_content = result.get("remind_content")
                
                if remind_time_str and remind_content:
                    await self._schedule_task(event, remind_time_str, remind_content)

        except Exception as e:
            logger.error(f"[SmartReminder] LLM analysis failed: {e}")

    # ==========================
    # 任务调度与执行
    # ==========================

    async def _schedule_task(self, event: AstrMessageEvent, time_str: str, content: str):
        """
        创建并持久化提醒任务
        """
        try:
            # 解析时间
            trigger_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            
            if trigger_time <= now:
                logger.warning(f"[SmartReminder] Trigger time {time_str} is in the past, ignoring.")
                return

            job_id = str(uuid.uuid4())[:8]
            
            # 保存任务元数据，用于在触发时重建事件
            task_data = {
                "id": job_id,
                "unified_msg_origin": event.unified_msg_origin,
                "sender_id": event.get_sender_id(),
                "sender_name": event.get_sender_name(),
                "group_id": event.get_group_id() or "",
                "platform_name": event.get_platform_name(),
                "content": content,
                "time": time_str
            }

            # 添加到调度器
            self.scheduler.add_job(
                self._trigger_callback,
                'date',
                run_date=trigger_time,
                args=[task_data],
                id=job_id
            )

            # 持久化
            self.tasks[job_id] = task_data
            self._save_tasks()
            
            logger.info(f"[SmartReminder] Scheduled task {job_id} at {time_str}: {content}")
            
        except ValueError:
            logger.error(f"[SmartReminder] Invalid time format: {time_str}. Expected YYYY-MM-DD HH:MM:SS")
        except Exception as e:
            logger.error(f"[SmartReminder] Schedule task failed: {e}")

async def _trigger_callback(self, task_data: dict):
        """
        任务触发回调：
        1. 获取当前对话历史。
        2. 将提醒内容作为 Prompt 传给 LLM（模拟用户此时发起了提醒请求）。
        3. 发送 LLM 的回复。
        4. 将 "伪造的提醒请求" 和 "LLM的回复" 一并写入历史记录，实现上下文注入。
        """
        try:
            job_id = task_data["id"]
            content = task_data["content"]
            unified_msg_origin = task_data.get("unified_msg_origin")
            
            logger.info(f"[SmartReminder] Triggering task {job_id}: {content}")

            # 1. 准备历史上下文
            cm = self.context.conversation_manager
            curr_cid = await cm.get_curr_conversation_id(unified_msg_origin)
            
            conversation = None
            history = []
            if curr_cid:
                conversation = await cm.get_conversation(unified_msg_origin, curr_cid)
                if conversation and conversation.history:
                    history = json.loads(conversation.history)

            # 2. 准备 LLM 调用
            # 这里的 trigger_text 既作为 Prompt 发给 LLM，后续也会存入历史
            # 这样 LLM 就会认为这是用户刚刚说的话，从而自然地进行回复
            trigger_text = f"现在是提醒所指的时间，请你根据你的设定（人格/角色），给用户 {sender_name} 发送一条提醒消息。\n提醒的具体事项是：{content}\n要求：\n1. 语气要自然、符合你的人格设定。\n2. 不要只重复事项，要像和朋友或主人说话一样,自然地接入当前场景。\n3. 如果事件已经结束或已经在进行中，忽略本次提醒，继续当前场景事件。\n4. 直接输出你要说的话，不要包含'好的'、'如下'等无关内容。"
            
            # 获取 Provider
            analysis_model_id = self.config.get("analysis_model_id", "")
            provider = None
            if analysis_model_id:
                provider = self.context.get_provider_by_id(analysis_model_id)
            if not provider:
                provider = self.context.get_using_provider()

            success = False

            if provider:
                try:
                    # 调用 LLM
                    # 此时传入的 contexts 是旧历史，prompt 是本次触发的内容
                    response = await provider.text_chat(
                        prompt=trigger_text,
                        contexts=history, 
                        session_id=None
                    )
                    
                    if response and response.completion_text:
                        reply = response.completion_text
                        
                        # --- 3. 仅发送 LLM 生成的回复 ---
                        await self.context.send_message(unified_msg_origin, MessageChain().message(reply))
                        
                        # --- 4. 注入历史上下文 (闭环) ---
                        if conversation:
                            # 模拟用户消息
                            history.append({"role": "user", "content": trigger_text})
                            # 记录 Bot 回复
                            history.append({"role": "assistant", "content": reply})
                            
                            # 保存到数据库
                            conversation.history = json.dumps(history, ensure_ascii=False)
                            await cm.save_conversation(conversation)
                        
                        success = True
                        
                except Exception as e:
                    logger.warning(f"[SmartReminder] LLM generation failed: {e}")

            # 任务完成后移除
            self._remove_task_internal(job_id)

    # ==========================
    # 指令处理
    # ==========================

    @filter.command_group("remind")
    def remind_group(self, event: AstrMessageEvent):
        """提醒任务管理指令"""
        pass

    @remind_group.command("list")
    async def list_tasks(self, event: AstrMessageEvent):
        """查看当前待执行的任务"""
        if not self.tasks:
            yield event.plain_result("当前没有待执行的提醒任务。")
            return

        result = ["📋 待执行提醒任务："]
        has_task = False
        for tid, task in self.tasks.items():
            # 简单过滤：只显示当前会话的任务
            if task.get("unified_msg_origin") == event.unified_msg_origin:
                result.append(f"🆔 {tid} | ⏰ {task['time']}")
                result.append(f"   内容: {task['content']}")
                result.append("-" * 20)
                has_task = True
        
        if not has_task:
            yield event.plain_result("当前会话没有待执行的提醒任务。")
        else:
            yield event.plain_result("\n".join(result))

    @remind_group.command("remove")
    async def remove_task(self, event: AstrMessageEvent, task_id: str):
        """删除指定ID的任务"""
        if task_id in self.tasks:
            # 权限检查：只能删除当前会话的任务
            if self.tasks[task_id].get("unified_msg_origin") != event.unified_msg_origin:
                yield event.plain_result("❌ 无法删除非当前会话的任务。")
                return
                
            self._remove_task_internal(task_id)
            yield event.plain_result(f"✅ 任务 {task_id} 已删除。")
        else:
            yield event.plain_result(f"❌ 未找到 ID 为 {task_id} 的任务。")

    @remind_group.command("add")
    async def add_task(self, event: AstrMessageEvent, time_desc: str, content: str):
        """
        手动添加任务
        /remind add "十分钟后" "去吃饭"
        """
        yield event.plain_result("正在解析提醒请求...")
        
        # 构造一个伪造的历史记录，让 LLM 解析
        fake_history = [
            {"role": "user", "content": f"请帮我设置一个提醒：{time_desc}提醒我{content}"}
        ]
        
        # 复用分析逻辑
        await self._analyze_and_schedule(event, fake_history)

    # ==========================
    # 辅助方法
    # ==========================

    def _extract_json(self, text: str) -> Optional[dict]:
        """从文本中提取 JSON"""
        try:
            # 寻找第一个 { 和最后一个 }
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                json_str = match.group()
                return json.loads(json_str)
            return None
        except Exception:
            return None

    def _load_tasks(self):
        """从文件加载任务"""
        if self.tasks_file.exists():
            try:
                with open(self.tasks_file, 'r', encoding='utf-8') as f:
                    self.tasks = json.load(f)
                
                # 恢复到调度器
                now = datetime.now()
                for tid, task in self.tasks.items():
                    try:
                        run_time = datetime.strptime(task["time"], "%Y-%m-%d %H:%M:%S")
                        if run_time > now:
                            self.scheduler.add_job(
                                self._trigger_callback,
                                'date',
                                run_date=run_time,
                                args=[task],
                                id=tid
                            )
                    except Exception as e:
                        logger.error(f"[SmartReminder] Failed to restore task {tid}: {e}")
            except Exception as e:
                logger.error(f"[SmartReminder] Failed to load tasks: {e}")

    def _save_tasks(self):
        """保存任务到文件"""
        try:
            with open(self.tasks_file, 'w', encoding='utf-8') as f:
                json.dump(self.tasks, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[SmartReminder] Failed to save tasks: {e}")

    def _remove_task_internal(self, job_id: str):
        """内部移除任务逻辑"""
        # 移除调度器中的作业
        try:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
        except Exception:
            pass
        
        # 移除内存和文件中的记录
        if job_id in self.tasks:
            del self.tasks[job_id]
            self._save_tasks()

    def _clean_expired_tasks(self):
        """清理已过期的任务记录"""
        now = datetime.now()
        expired = []
        for tid, task in self.tasks.items():
            try:
                run_time = datetime.strptime(task["time"], "%Y-%m-%d %H:%M:%S")
                if run_time <= now:
                    expired.append(tid)
            except ValueError:
                expired.append(tid)
        
        for tid in expired:
            self._remove_task_internal(tid)
        
        if expired:
            logger.info(f"[SmartReminder] Cleaned {len(expired)} expired tasks.")
