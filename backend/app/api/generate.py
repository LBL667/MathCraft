"""参数化快速出题路由。"""
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException

from app.schemas.question import GenerateParams
from app.services.question_gen import NeedConfirmError, generate_questions
from app.services.vectorstore import list_documents

router = APIRouter(prefix="/api", tags=["generate"])

# 生成任务注册表：task_id -> 状态。真实进度由生成流程内部打点，轮询端点只读。
# done 后结果保留 10 分钟供前端容错拉取，随后清理。
TASKS: dict[str, dict] = {}
TASK_TTL = 600


def _cleanup_expired() -> None:
    now = time.time()
    expired = [k for k, v in TASKS.items() if v.get("done") and now - v.get("finished_at", 0) > TASK_TTL]
    for k in expired:
        TASKS.pop(k, None)


def _run_task(task_id: str, params: GenerateParams, doc_name: str) -> None:
    task = TASKS[task_id]

    def cb(progress: int, stage: str) -> None:
        task["progress"] = max(task["progress"], int(progress))
        task["stage"] = stage

    try:
        questions = generate_questions(
            params,
            doc_name,
            scope=params.scope or None,
            existing_stems=params.existing_stems,
            progress_cb=cb,
        )
        task["progress"] = 100
        task["stage"] = "完成"
        task["questions"] = [q.model_dump() for q in questions]
    except NeedConfirmError as exc:
        task["error"] = {
            "kind": "need_confirm",
            "message": f"材料中「{exc.type_label}」的原题只有 {exc.available} 道，不足 {exc.requested} 道。"
            "可以减少数量，或把来源模式改为改数据、改背景、全新题。",
            "type_label": exc.type_label,
            "available": exc.available,
            "requested": exc.requested,
        }
    except ValueError as exc:
        task["error"] = {"message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        task["error"] = {"message": f"生成失败：{exc}"}
    finally:
        task["done"] = True
        task["finished_at"] = time.time()


@router.post("/generate")
def generate(params: GenerateParams):
    """同步出题（保留旧行为，供脚本与其他调用方使用）。"""
    docs = list_documents(params.kb_id)
    doc_name = next((d["doc_name"] for d in docs if d["doc_id"] == params.doc_id), params.doc_id)
    try:
        questions = generate_questions(
            params,
            doc_name,
            scope=params.scope or None,
            existing_stems=params.existing_stems,
        )
    except NeedConfirmError as exc:
        # 原题数量不足：422 + 结构化信息，前端提示用户选择处理方式
        raise HTTPException(
            422,
            detail={
                "kind": "need_confirm",
                "message": f"材料中「{exc.type_label}」的原题只有 {exc.available} 道，不足 {exc.requested} 道。",
                "type_label": exc.type_label,
                "available": exc.available,
                "requested": exc.requested,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"生成失败：{exc}") from exc
    return [q.model_dump() for q in questions]


@router.post("/generate/start")
def start_generate(params: GenerateParams):
    """异步出题：创建任务立即返回 task_id，前端轮询 /generate/progress 获取真实进度。"""
    _cleanup_expired()
    task_id = uuid.uuid4().hex
    TASKS[task_id] = {"progress": 2, "stage": "任务已创建", "done": False, "questions": None, "error": None}
    docs = list_documents(params.kb_id)
    doc_name = next((d["doc_name"] for d in docs if d["doc_id"] == params.doc_id), params.doc_id)
    thread = threading.Thread(target=_run_task, args=(task_id, params, doc_name), daemon=True)
    thread.start()
    return {"task_id": task_id}


@router.get("/generate/progress/{task_id}")
def generate_progress(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在或已过期")
    return {
        "progress": task["progress"],
        "stage": task["stage"],
        "done": task["done"],
        "questions": task["questions"],
        "error": task["error"],
    }
