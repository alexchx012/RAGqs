"""RAG Agent 服务 - 基于 LangGraph 的知识库问答"""

from collections.abc import AsyncGenerator, Sequence
from typing import Annotated, Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph.message import add_messages
from loguru import logger
from typing_extensions import TypedDict

from app.agents import (
    ChatModelAnswerGenerator,
    LangChainToolExecutor,
    LangChainToolPlanner,
    ToolExecutor,
    ToolPlanner,
    build_rag_state_graph,
)
from app.config import config
from app.extensions.tools import ToolRegistry, build_enabled_tools, parse_enabled_tool_names
from app.observability import get_current_trace_id
from app.observability.retrieval_audit import RetrievalAuditRecord
from app.prompts.profiles import build_system_prompt
from app.providers.contracts import (
    ChatModelProvider,
    CheckpointProvider,
    RetrievalAuditStoreProvider,
    RetrievalRequest,
    RetrievalResult,
    RetrievalSource,
    RetrieverProvider,
    SessionStoreProvider,
    SessionSummary,
    StoredMessage,
)
from app.tools.knowledge_tool import enforce_knowledge_space


class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


class RagAgentService:
    """RAG Agent 服务 - 纯知识库问答"""

    def __init__(
        self,
        streaming: bool = True,
        chat_model_provider: ChatModelProvider | None = None,
        agent_factory=create_agent,
        tools: list | None = None,
        checkpointer=None,
        checkpoint_provider: CheckpointProvider | None = None,
        retriever_provider: RetrieverProvider | None = None,
        session_store_provider: SessionStoreProvider | None = None,
        retrieval_audit_store_provider: RetrievalAuditStoreProvider | None = None,
        retrieval_top_k: int | None = None,
        use_explicit_graph: bool | None = None,
        explicit_graph: Any | None = None,
        agent_runtime: str | None = None,
        prompt_profile: str | None = None,
        system_prompt: str | None = None,
        enabled_tool_names: list[str] | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        tool_planner: ToolPlanner | None = None,
        tool_planning_enabled: bool | None = None,
    ):
        self.model_name = config.rag_model
        self.streaming = streaming
        self.system_prompt = system_prompt or self._build_system_prompt(prompt_profile)

        self.chat_model_provider = chat_model_provider or self._get_chat_model_provider()
        self.agent_factory = agent_factory
        self.model = None
        self.tools = tools if tools is not None else build_enabled_tools(
            enabled_tool_names if enabled_tool_names is not None else config.enabled_tools,
            registry=tool_registry,
        )
        self.tool_executor = tool_executor or LangChainToolExecutor(self.tools)
        self.tool_planning_enabled = (
            config.tool_planning_enabled
            if tool_planning_enabled is None
            else tool_planning_enabled
        )
        self.tool_planner = tool_planner or self._build_tool_planner()
        self.checkpoint_provider = checkpoint_provider
        self.checkpointer = (
            checkpointer
            if checkpointer is not None
            else self._get_checkpoint_provider().create_checkpointer()
        )
        self.retriever_provider = retriever_provider
        self.session_store_provider = session_store_provider
        self.retrieval_audit_store_provider = retrieval_audit_store_provider
        self.retrieval_top_k = retrieval_top_k or config.rag_top_k
        self.agent_runtime = _normalize_agent_runtime(agent_runtime or config.agent_runtime)
        self.use_explicit_graph = (
            self.agent_runtime == "explicit_graph"
            if use_explicit_graph is None
            else use_explicit_graph
        )
        self.explicit_graph = explicit_graph
        self.agent = None
        self._agent_initialized = False

        logger.info(f"RAG Agent 初始化完成, model={self.model_name}")

    async def _initialize_agent(self):
        if self._agent_initialized:
            return
        if self.model is None:
            self.model = self.chat_model_provider.create_chat_model(streaming=self.streaming)
        self.agent = self.agent_factory(self.model, tools=self.tools, checkpointer=self.checkpointer)
        self._agent_initialized = True
        tool_names = [t.name for t in self.tools]
        logger.info(f"Agent 工具: {', '.join(tool_names)}")

    def _build_system_prompt(self, prompt_profile: str | None = None) -> str:
        return build_system_prompt(prompt_profile or config.prompt_profile)

    async def query(self, question: str, session_id: str, space_id: str = "default") -> str:
        """非流式问答"""
        try:
            if self.use_explicit_graph:
                state = self._invoke_explicit_graph(question, session_id, space_id=space_id)
                answer = state.get("answer", "")
                self._record_session_exchange(
                    session_id=session_id,
                    question=question,
                    answer=answer,
                    assistant_metadata=_session_metadata_from_graph_state(state),
                )
                return answer

            answer = await self._run_legacy_query(question, session_id, space_id=space_id)
            self._record_session_exchange(session_id=session_id, question=question, answer=answer)
            return answer
        except Exception as e:
            logger.error(f"[会话 {session_id}] 查询失败: {e}")
            raise

    async def query_with_trace(
        self,
        question: str,
        session_id: str,
        space_id: str = "default",
    ) -> dict[str, Any]:
        """非流式问答，附带检索引用和调试信息。"""
        if self.use_explicit_graph:
            state = self._invoke_explicit_graph(question, session_id, space_id=space_id)
            result = _serialize_graph_state(question, state)
            self._record_session_exchange(
                session_id=session_id,
                question=question,
                answer=result["answer"],
                assistant_metadata=_session_metadata_from_trace(result),
            )
            self._record_retrieval_audit(
                session_id=session_id,
                space_id=space_id,
                question=question,
                trace=result,
            )
            return result
        retrieval_result = self.retrieve_context(question, space_id=space_id)
        answer = await self._run_legacy_query(
            question,
            session_id=session_id,
            space_id=space_id,
        )
        result = {
            "answer": answer,
            "sources": _serialize_sources(retrieval_result.sources),
            "retrieval": _serialize_retrieval_result(retrieval_result),
        }
        self._record_session_exchange(
            session_id=session_id,
            question=question,
            answer=answer,
            assistant_metadata=_session_metadata_from_trace(result),
        )
        self._record_retrieval_audit(
            session_id=session_id,
            space_id=space_id,
            question=question,
            trace=result,
        )
        return result

    async def query_stream(
        self,
        question: str,
        session_id: str,
        space_id: str = "default",
    ) -> AsyncGenerator[dict[str, Any], None]:
        """流式问答"""
        try:
            if self.use_explicit_graph:
                state: dict[str, Any] = {}
                for chunk in self._stream_explicit_graph(
                    question,
                    session_id,
                    space_id=space_id,
                    final_state=state,
                ):
                    yield chunk
                self._record_session_exchange(
                    session_id=session_id,
                    question=question,
                    answer=state.get("answer", ""),
                    assistant_metadata=_session_metadata_from_graph_state(state),
                )
                return
            await self._initialize_agent()
            messages = [SystemMessage(content=self.system_prompt), HumanMessage(content=question)]
            config_dict = {"configurable": {"thread_id": session_id}}
            answer_parts: list[str] = []

            with enforce_knowledge_space(space_id):
                async for token, metadata in self.agent.astream(
                    input={"messages": messages}, config=config_dict, stream_mode="messages",
                ):
                    node_name = metadata.get('langgraph_node', 'unknown') if isinstance(metadata, dict) else 'unknown'
                    message_type = type(token).__name__
                    if message_type in ("AIMessage", "AIMessageChunk"):
                        content_blocks = getattr(token, 'content_blocks', None)
                        if content_blocks and isinstance(content_blocks, list):
                            for block in content_blocks:
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    text_content = block.get('text', '')
                                    if text_content:
                                        answer_parts.append(text_content)
                                        yield {"type": "content", "data": text_content, "node": node_name}

            yield {"type": "complete"}
            self._record_session_exchange(
                session_id=session_id,
                question=question,
                answer="".join(answer_parts),
            )
        except Exception as e:
            logger.error(f"[会话 {session_id}] 流式查询失败: {e}")
            yield {"type": "error", "data": str(e)}
            raise

    async def query_stream_with_trace(
        self,
        question: str,
        session_id: str,
        space_id: str = "default",
    ) -> AsyncGenerator[dict[str, Any], None]:
        if self.use_explicit_graph:
            state: dict[str, Any] = {}
            for chunk in self._stream_explicit_graph(
                question,
                session_id,
                space_id=space_id,
                final_state=state,
            ):
                yield chunk
            trace = _serialize_graph_state(question, state)
            self._record_session_exchange(
                session_id=session_id,
                question=question,
                answer=trace["answer"],
                assistant_metadata=_session_metadata_from_trace(trace),
            )
            self._record_retrieval_audit(
                session_id=session_id,
                space_id=space_id,
                question=question,
                trace=trace,
            )
            return
        retrieval_result = self.retrieve_context(question, space_id=space_id)
        retrieval = _serialize_retrieval_result(retrieval_result)
        yield {"type": "retrieval", "data": retrieval}
        answer_parts: list[str] = []
        async for chunk in self.query_stream(question, session_id=session_id, space_id=space_id):
            if chunk.get("type") in {"content", "token"} and isinstance(chunk.get("data"), str):
                answer_parts.append(chunk["data"])
            yield chunk
        self._record_retrieval_audit(
            session_id=session_id,
            space_id=space_id,
            question=question,
            trace={
                "answer": "".join(answer_parts),
                "sources": retrieval["sources"],
                "retrieval": retrieval,
            },
        )

    def _invoke_explicit_graph(
        self,
        question: str,
        session_id: str,
        space_id: str = "default",
    ) -> dict[str, Any]:
        if self.explicit_graph is None:
            self.explicit_graph = self._build_default_explicit_graph()
        return self.explicit_graph.invoke(
            {"question": question, "session_id": session_id, "space_id": space_id},
            {"configurable": {"thread_id": session_id}},
        )

    def _stream_explicit_graph(
        self,
        question: str,
        session_id: str,
        space_id: str = "default",
        final_state: dict[str, Any] | None = None,
    ):
        if self.explicit_graph is None:
            self.explicit_graph = self._build_default_explicit_graph()

        input_payload = {"question": question, "session_id": session_id, "space_id": space_id}
        config_dict = {"configurable": {"thread_id": session_id}}
        target_state = final_state if final_state is not None else {}

        if not hasattr(self.explicit_graph, "stream"):
            state = self.explicit_graph.invoke(input_payload, config_dict)
            target_state.update(state)
            yield from _stream_chunks_from_graph_state(state)
            return

        has_done_event = False
        for mode, payload in self.explicit_graph.stream(
            input_payload,
            config_dict,
            stream_mode=["custom", "updates"],
        ):
            if mode == "custom":
                chunk = _stream_chunk_from_custom_payload(payload)
                if chunk is not None:
                    yield chunk
                continue
            if mode != "updates":
                continue
            for update in _graph_update_values(payload):
                target_state.update(update)
                for chunk in _stream_chunks_from_graph_update(update):
                    if chunk["type"] == "done":
                        has_done_event = True
                    yield chunk

        if not has_done_event:
            yield {"type": "done", "data": {"answer": target_state.get("answer", "")}}

    def _build_default_explicit_graph(self):
        return build_rag_state_graph(
            retriever_provider=self._get_retriever_provider(),
            answer_generator=ChatModelAnswerGenerator(
                chat_model_provider=self.chat_model_provider,
                system_prompt=self.system_prompt,
            ),
            tool_executor=self.tool_executor,
            tool_planner=self.tool_planner,
            checkpointer=self.checkpointer,
            default_top_k=self.retrieval_top_k,
        )

    def retrieve_context(self, question: str, space_id: str = "default") -> RetrievalResult:
        retriever_provider = self._get_retriever_provider()
        filters = {"space_id": space_id} if space_id and space_id != "default" else {}
        return retriever_provider.retrieve(
            RetrievalRequest(query=question, top_k=self.retrieval_top_k, filters=filters)
        )

    async def _run_legacy_query(
        self,
        question: str,
        session_id: str,
        space_id: str = "default",
    ) -> str:
        await self._initialize_agent()
        messages = [SystemMessage(content=self.system_prompt), HumanMessage(content=question)]
        config_dict = {"configurable": {"thread_id": session_id}}
        with enforce_knowledge_space(space_id):
            result = await self.agent.ainvoke(input={"messages": messages}, config=config_dict)
        messages_result = result.get("messages", [])
        if messages_result:
            last_message = messages_result[-1]
            return last_message.content if hasattr(last_message, 'content') else str(last_message)
        return ""

    def _get_retriever_provider(self) -> RetrieverProvider:
        if self.retriever_provider is None:
            from app.providers.factory import get_default_provider_container

            self.retriever_provider = get_default_provider_container().retriever_provider
        return self.retriever_provider

    def _get_chat_model_provider(self) -> ChatModelProvider:
        from app.providers.factory import get_default_provider_container

        return get_default_provider_container().chat_model_provider

    def _get_session_store_provider(self) -> SessionStoreProvider:
        if self.session_store_provider is None:
            from app.providers.factory import get_default_provider_container

            self.session_store_provider = get_default_provider_container().session_store_provider
        return self.session_store_provider

    def _get_retrieval_audit_store_provider(self) -> RetrievalAuditStoreProvider:
        if self.retrieval_audit_store_provider is None:
            from app.providers.factory import get_default_provider_container

            self.retrieval_audit_store_provider = (
                get_default_provider_container().retrieval_audit_store_provider
            )
        return self.retrieval_audit_store_provider

    def _get_checkpoint_provider(self) -> CheckpointProvider:
        if self.checkpoint_provider is None:
            from app.providers.factory import get_default_provider_container

            self.checkpoint_provider = get_default_provider_container().checkpoint_provider
        return self.checkpoint_provider

    def _build_tool_planner(self) -> ToolPlanner | None:
        if not self.tool_planning_enabled:
            return None
        planner_tools = _filter_tool_planning_tools(
            self.tools,
            excluded_tool_names=config.tool_planning_excluded_tools,
        )
        return LangChainToolPlanner(
            chat_model_provider=self.chat_model_provider,
            tools=planner_tools,
            system_prompt=self.system_prompt,
        )

    def _record_session_exchange(
        self,
        *,
        session_id: str,
        question: str,
        answer: str,
        assistant_metadata: dict[str, Any] | None = None,
    ) -> None:
        session_store = self._get_session_store_provider()
        session_store.append_message(session_id, "user", question)
        session_store.append_message(session_id, "assistant", answer, assistant_metadata)

    def _record_retrieval_audit(
        self,
        *,
        session_id: str,
        space_id: str,
        question: str,
        trace: dict[str, Any],
    ) -> None:
        try:
            record = RetrievalAuditRecord(
                trace_id=get_current_trace_id() or str(uuid4()),
                session_id=session_id,
                space_id=space_id,
                question=question,
                answer=str(trace.get("answer", "")),
                sources=list(trace.get("sources", [])),
                retrieval=dict(trace.get("retrieval", {})),
            )
            self._get_retrieval_audit_store_provider().append(record)
        except Exception as exc:
            logger.warning(f"记录检索审计失败: {exc}")

    def get_session_history(self, session_id: str) -> list:
        session_store = self._get_session_store_provider()
        stored_messages = session_store.get_messages(session_id)
        if stored_messages:
            return _serialize_stored_messages(stored_messages)

        try:
            cfg = {"configurable": {"thread_id": session_id}}
            checkpoint_tuple = self.checkpointer.get(cfg)
            if not checkpoint_tuple:
                return []
            if hasattr(checkpoint_tuple, 'checkpoint'):
                checkpoint_data = checkpoint_tuple.checkpoint
            else:
                checkpoint_data = checkpoint_tuple[0] if checkpoint_tuple else {}
            messages = checkpoint_data.get("channel_values", {}).get("messages", [])
            history = []
            for msg in messages:
                if isinstance(msg, SystemMessage):
                    continue
                role = "user" if isinstance(msg, HumanMessage) else "assistant"
                content = msg.content if hasattr(msg, 'content') else str(msg)
                from datetime import datetime
                history.append({"role": role, "content": content, "timestamp": datetime.now().isoformat()})
            return history
        except Exception as e:
            logger.error(f"获取会话历史失败: {session_id}, 错误: {e}")
            return []

    def list_sessions(self, query: str | None = None) -> list[SessionSummary]:
        session_store = self._get_session_store_provider()
        return session_store.list_sessions(query=query)

    def list_retrieval_audits(
        self,
        *,
        session_id: str | None = None,
        space_id: str | None = None,
        trace_id: str | None = None,
        limit: int = 50,
    ) -> list[RetrievalAuditRecord]:
        return self._get_retrieval_audit_store_provider().list_records(
            session_id=session_id,
            space_id=space_id,
            trace_id=trace_id,
            limit=limit,
        )

    def clear_session(self, session_id: str) -> bool:
        try:
            success = self._get_session_store_provider().clear(session_id)
            try:
                self.checkpointer.delete_thread(session_id)
            except AttributeError:
                pass
            return success
        except Exception as e:
            logger.error(f"清空会话历史失败: {session_id}, 错误: {e}")
            return False


def _normalize_agent_runtime(agent_runtime: str) -> str:
    normalized = str(agent_runtime).strip().lower().replace("-", "_")
    if normalized not in {"explicit_graph", "legacy"}:
        raise ValueError(f"unsupported agent runtime: {agent_runtime}")
    return normalized


def _filter_tool_planning_tools(
    tools: list[Any],
    *,
    excluded_tool_names: str,
) -> list[Any]:
    excluded = set(parse_enabled_tool_names(excluded_tool_names))
    return [
        tool
        for tool in tools
        if (getattr(tool, "name", None) or getattr(tool, "__name__", "")) not in excluded
    ]


rag_agent_service = RagAgentService(streaming=True)


def _serialize_retrieval_result(result: RetrievalResult) -> dict[str, Any]:
    return {
        "query": result.query,
        "rewrittenQuery": result.rewritten_query,
        "sources": _serialize_sources(result.sources),
        "debug": result.debug,
    }


def _serialize_sources(sources: list[RetrievalSource]) -> list[dict[str, Any]]:
    return [
        {
            "index": source.index,
            "sourcePath": source.source_path,
            "fileName": source.file_name,
            "headingPath": source.heading_path,
            "chunkId": source.chunk_id,
            "documentId": source.document_id,
            "score": source.score,
        }
        for source in sources
    ]


def _serialize_stored_messages(messages: list[StoredMessage]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for message in messages:
        history.append(
            {
                "role": message.role,
                "content": message.content,
                "metadata": dict(message.metadata),
                "timestamp": message.created_at,
            }
        )
    return history


def _serialize_graph_state(question: str, state: dict[str, Any]) -> dict[str, Any]:
    sources = list(state.get("sources", []))
    retrieval_debug = dict(state.get("retrieval_debug", {}))
    return {
        "answer": state.get("answer", ""),
        "sources": sources,
        "retrieval": {
            "query": state.get("normalized_question") or question,
            "rewrittenQuery": state.get("rewritten_query"),
            "sources": sources,
            "debug": retrieval_debug,
        },
    }


def _stream_chunks_from_graph_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    has_done_event = False
    for event in state.get("events", []):
        chunk = _stream_chunk_from_graph_event(event)
        if chunk is None:
            continue
        if chunk["type"] == "done":
            has_done_event = True
        chunks.append(chunk)
    if not has_done_event:
        chunks.append({"type": "done", "data": {"answer": state.get("answer", "")}})
    return chunks


def _stream_chunks_from_graph_update(update: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for event in update.get("events", []):
        chunk = _stream_chunk_from_graph_event(event)
        if chunk is not None:
            chunks.append(chunk)
    return chunks


def _stream_chunk_from_graph_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    if event_type in {"__reset_events__", "input", "answer"}:
        return None
    chunk = {"type": event_type, "data": event.get("data")}
    if "node" in event:
        chunk["node"] = event["node"]
    return chunk


def _stream_chunk_from_custom_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        chunk_type = payload.get("type", "custom")
        chunk = {"type": chunk_type, "data": payload.get("data")}
        if "node" in payload:
            chunk["node"] = payload["node"]
        return chunk
    return {"type": "custom", "data": payload}


def _graph_update_values(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    if payload and all(isinstance(value, dict) for value in payload.values()):
        return list(payload.values())
    return [payload]


def _session_metadata_from_trace(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "sources": trace.get("sources", []),
        "retrieval": trace.get("retrieval", {}),
    }


def _session_metadata_from_graph_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "sources": list(state.get("sources", [])),
        "retrieval": {
            "query": state.get("normalized_question") or state.get("question", ""),
            "rewrittenQuery": state.get("rewritten_query"),
            "sources": list(state.get("sources", [])),
            "debug": dict(state.get("retrieval_debug", {})),
        },
    }
