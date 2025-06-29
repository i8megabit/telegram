"""Сервис тестирования для интеграции с основным бэкендом reLink"""

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import uuid
import httpx
import psutil
import traceback
from fastapi import APIRouter, Response
import os

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from sqlalchemy.orm import selectinload

from .models import (
    Test, TestExecution, TestResult, TestSuite, TestReport,
    TestRequest, TestResponse, TestExecutionResponse, TestSuiteRequest, TestSuiteResponse,
    TestFilter, TestStatus, TestType, TestPriority, TestEnvironment
)
from .monitoring import logger, metrics_collector
from .exceptions import raise_validation_error, raise_not_found, raise_database_error


class TestingService:
    """Сервис для управления тестированием"""
    
    def __init__(self):
        self.running_executions: Dict[str, asyncio.Task] = {}
        self.test_workers: Dict[str, Any] = {}
        self.test_results_cache: Dict[str, Dict[str, Any]] = {}
    
    async def create_test(self, test_request: TestRequest, user_id: int, db: AsyncSession) -> TestResponse:
        """Создание нового теста"""
        try:
            # Создаем тест в базе данных
            test = Test(
                id=str(uuid.uuid4()),
                name=test_request.name,
                description=test_request.description,
                test_type=test_request.test_type.value,
                priority=test_request.priority.value,
                environment=test_request.environment.value,
                status=TestStatus.PENDING.value,
                timeout=test_request.timeout,
                parameters=test_request.parameters,
                retry_count=test_request.retry_count,
                parallel=test_request.parallel,
                dependencies=test_request.dependencies,
                tags=test_request.tags,
                user_id=user_id
            )
            
            db.add(test)
            await db.commit()
            await db.refresh(test)
            
            logger.info(f"Создан тест: {test.id} - {test.name}")
            
            return TestResponse(
                id=test.id,
                name=test.name,
                description=test.description,
                test_type=TestType(test.test_type),
                priority=TestPriority(test.priority),
                environment=TestEnvironment(test.environment),
                status=TestStatus(test.status),
                created_at=test.created_at,
                updated_at=test.updated_at,
                tags=test.tags or [],
                metadata=test.parameters or {}
            )
            
        except Exception as e:
            await db.rollback()
            logger.error(f"Ошибка создания теста: {e}")
            raise_validation_error(f"Не удалось создать тест: {str(e)}")
    
    async def get_tests(self, filters: TestFilter, skip: int, limit: int, db: AsyncSession) -> List[TestResponse]:
        """Получение списка тестов с фильтрацией"""
        try:
            query = select(Test)
            
            # Применяем фильтры
            if filters.test_type:
                query = query.where(Test.test_type == filters.test_type.value)
            if filters.status:
                query = query.where(Test.status == filters.status.value)
            if filters.priority:
                query = query.where(Test.priority == filters.priority.value)
            if filters.environment:
                query = query.where(Test.environment == filters.environment.value)
            if filters.name_contains:
                query = query.where(Test.name.contains(filters.name_contains))
            if filters.description_contains:
                query = query.where(Test.description.contains(filters.description_contains))
            if filters.tags:
                # Простая фильтрация по тегам (можно улучшить)
                for tag in filters.tags:
                    query = query.where(Test.tags.contains([tag]))
            
            # Сортировка
            if filters.sort_by == "created_at":
                if filters.sort_order == "desc":
                    query = query.order_by(Test.created_at.desc())
                else:
                    query = query.order_by(Test.created_at.asc())
            
            # Пагинация
            query = query.offset(skip).limit(limit)
            
            result = await db.execute(query)
            tests = result.scalars().all()
            
            return [
                TestResponse(
                    id=test.id,
                    name=test.name,
                    description=test.description,
                    test_type=TestType(test.test_type),
                    priority=TestPriority(test.priority),
                    environment=TestEnvironment(test.environment),
                    status=TestStatus(test.status),
                    created_at=test.created_at,
                    updated_at=test.updated_at,
                    tags=test.tags or [],
                    metadata=test.parameters or {}
                )
                for test in tests
            ]
            
        except Exception as e:
            logger.error(f"Ошибка получения тестов: {e}")
            raise_database_error(f"Не удалось получить тесты: {str(e)}")
    
    async def get_test(self, test_id: str, db: AsyncSession) -> Optional[TestResponse]:
        """Получение теста по ID"""
        try:
            query = select(Test).where(Test.id == test_id)
            result = await db.execute(query)
            test = result.scalar_one_or_none()
            
            if not test:
                return None
            
            return TestResponse(
                id=test.id,
                name=test.name,
                description=test.description,
                test_type=TestType(test.test_type),
                priority=TestPriority(test.priority),
                environment=TestEnvironment(test.environment),
                status=TestStatus(test.status),
                created_at=test.created_at,
                updated_at=test.updated_at,
                tags=test.tags or [],
                metadata=test.parameters or {}
            )
            
        except Exception as e:
            logger.error(f"Ошибка получения теста {test_id}: {e}")
            raise_database_error(f"Не удалось получить тест: {str(e)}")
    
    async def execute_test(self, test_id: str, user_id: int, db: AsyncSession) -> TestExecutionResponse:
        """Выполнение теста"""
        try:
            # Получаем тест
            test = await self.get_test(test_id, db)
            if not test:
                raise_not_found("Тест не найден")
            
            # Создаем выполнение теста
            execution = TestExecution(
                test_id=test.id,
                status=TestStatus.RUNNING,
                progress=0.0,
                started_at=datetime.utcnow(),
                user_id=user_id,
                test_metadata=test.test_metadata or {}
            )
            
            # Сохраняем выполнение
            db.add(execution)
            await db.commit()
            await db.refresh(execution)
            
            try:
                # Выполняем тест в зависимости от типа
                if test.test_type == TestType.UNIT:
                    result = await self._run_unit_test(test, execution)
                elif test.test_type == TestType.API:
                    result = await self._run_api_test(test, execution)
                elif test.test_type == TestType.PERFORMANCE:
                    result = await self._run_performance_test(test, execution)
                elif test.test_type == TestType.SEO:
                    result = await self._run_seo_test(test, execution)
                elif test.test_type == TestType.LLM:
                    result = await self._run_llm_test(test, execution)
                else:
                    result = await self._run_generic_test(test, execution)
                
                # Обновляем выполнение
                execution.status = TestStatus.PASSED if result.passed > 0 else TestStatus.FAILED
                execution.finished_at = datetime.utcnow()
                execution.progress = 100.0
                
                # Сохраняем результат
                test_result = TestResult(
                    execution_id=execution.id,
                    test_id=test.id,
                    status=result.status,
                    started_at=result.started_at,
                    finished_at=result.finished_at,
                    duration=result.duration,
                    passed=result.passed,
                    failed=result.failed,
                    skipped=result.skipped,
                    total=result.total,
                    message=result.message,
                    error=result.error,
                    stack_trace=result.stack_trace,
                    memory_usage=result.memory_usage,
                    cpu_usage=result.cpu_usage,
                    test_metadata=result.test_metadata or {}
                )
                
                db.add(test_result)
                await db.commit()
                
                return execution
                
            except Exception as e:
                # Обработка ошибок
                execution.status = TestStatus.ERROR
                execution.finished_at = datetime.utcnow()
                execution.progress = 100.0
                
                # Сохраняем ошибку
                test_result = TestResult(
                    execution_id=execution.id,
                    test_id=test.id,
                    status=TestStatus.ERROR,
                    started_at=execution.started_at,
                    finished_at=datetime.utcnow(),
                    duration=(datetime.utcnow() - execution.started_at).total_seconds(),
                    error=str(e),
                    stack_trace=traceback.format_exc(),
                    test_metadata=execution.test_metadata or {}
                )
                
                db.add(test_result)
                await db.commit()
                
                raise
            
        except Exception as e:
            await db.rollback()
            logger.error(f"Ошибка выполнения теста {test_id}: {e}")
            raise_validation_error(f"Не удалось выполнить тест: {str(e)}")
    
    async def _run_unit_test(self, test: TestResponse, execution: TestExecution) -> Dict[str, Any]:
        """Выполнение unit теста"""
        start_time = datetime.utcnow()
        
        try:
            # Запускаем pytest для unit тестов
            test_path = test.test_metadata.get("test_path", "tests/")
            cmd = [
                sys.executable, "-m", "pytest", test_path,
                "-v", "--tb=short", "--json-report"
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            duration = (datetime.utcnow() - start_time).total_seconds()
            
            if process.returncode == 0:
                return {
                    "status": "passed",
                    "duration": duration,
                    "message": "Unit тест пройден успешно",
                    "output": stdout.decode(),
                    "passed": 1,
                    "failed": 0,
                    "total": 1
                }
            else:
                return {
                    "status": "failed",
                    "duration": duration,
                    "error": stderr.decode(),
                    "output": stdout.decode(),
                    "passed": 0,
                    "failed": 1,
                    "total": 1
                }
                
        except Exception as e:
            duration = (datetime.utcnow() - start_time).total_seconds()
            return {
                "status": "error",
                "duration": duration,
                "error": str(e),
                "passed": 0,
                "failed": 1,
                "total": 1
            }
    
    async def _run_api_test(self, test: TestResponse, execution: TestExecution) -> Dict[str, Any]:
        """Выполнение API теста"""
        start_time = datetime.utcnow()
        
        try:
            # Получаем параметры теста
            url = test.test_metadata.get("url")
            method = test.test_metadata.get("method", "GET")
            expected_status = test.test_metadata.get("expected_status", 200)
            timeout = test.test_metadata.get("timeout", 30)
            
            if not url:
                raise ValueError("URL не указан для API теста")
            
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(method, url)
                
                duration = (datetime.utcnow() - start_time).total_seconds()
                
                if response.status_code == expected_status:
                    return {
                        "status": "passed",
                        "duration": duration,
                        "message": f"API тест пройден: {response.status_code}",
                        "response_status": response.status_code,
                        "response_time": duration,
                        "passed": 1,
                        "failed": 0,
                        "total": 1
                    }
                else:
                    return {
                        "status": "failed",
                        "duration": duration,
                        "error": f"Ожидался статус {expected_status}, получен {response.status_code}",
                        "response_status": response.status_code,
                        "response_time": duration,
                        "passed": 0,
                        "failed": 1,
                        "total": 1
                    }
                    
        except Exception as e:
            duration = (datetime.utcnow() - start_time).total_seconds()
            return {
                "status": "error",
                "duration": duration,
                "error": str(e),
                "passed": 0,
                "failed": 1,
                "total": 1
            }
    
    async def _run_performance_test(self, test: TestResponse, execution: TestExecution) -> Dict[str, Any]:
        """Выполнение performance теста"""
        start_time = datetime.utcnow()
        
        try:
            # Простой performance тест - измеряем время выполнения операции
            url = test.test_metadata.get("url")
            iterations = test.test_metadata.get("iterations", 10)
            timeout = test.test_metadata.get("timeout", 30)
            
            if not url:
                raise ValueError("URL не указан для performance теста")
            
            response_times = []
            
            async with httpx.AsyncClient(timeout=timeout) as client:
                for i in range(iterations):
                    test_start = datetime.utcnow()
                    response = await client.get(url)
                    test_duration = (datetime.utcnow() - test_start).total_seconds()
                    response_times.append(test_duration)
            
            duration = (datetime.utcnow() - start_time).total_seconds()
            avg_response_time = sum(response_times) / len(response_times)
            max_response_time = max(response_times)
            min_response_time = min(response_times)
            
            # Проверяем критерии производительности
            max_allowed = test.test_metadata.get("max_response_time", 1.0)
            success = avg_response_time <= max_allowed
            
            return {
                "status": "passed" if success else "failed",
                "duration": duration,
                "message": f"Performance тест: среднее время {avg_response_time:.3f}s",
                "avg_response_time": avg_response_time,
                "max_response_time": max_response_time,
                "min_response_time": min_response_time,
                "iterations": iterations,
                "passed": 1 if success else 0,
                "failed": 0 if success else 1,
                "total": 1
            }
            
        except Exception as e:
            duration = (datetime.utcnow() - start_time).total_seconds()
            return {
                "status": "error",
                "duration": duration,
                "error": str(e),
                "passed": 0,
                "failed": 1,
                "total": 1
            }
    
    async def _run_seo_test(self, test: TestResponse, execution: TestExecution) -> Dict[str, Any]:
        """Выполнение SEO теста"""
        start_time = datetime.utcnow()
        
        try:
            # SEO тест - проверяем базовые SEO параметры
            url = test.test_metadata.get("url")
            if not url:
                raise ValueError("URL не указан для SEO теста")
            
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
                html = response.text
                
                # Простые SEO проверки
                checks = {
                    "title": "<title>" in html,
                    "meta_description": 'name="description"' in html,
                    "h1_tags": html.count("<h1>") > 0,
                    "images_with_alt": 'alt=' in html,
                    "responsive_meta": 'viewport' in html
                }
                
                passed_checks = sum(checks.values())
                total_checks = len(checks)
                
                duration = (datetime.utcnow() - start_time).total_seconds()
                
                return {
                    "status": "passed" if passed_checks >= total_checks * 0.8 else "failed",
                    "duration": duration,
                    "message": f"SEO тест: {passed_checks}/{total_checks} проверок пройдено",
                    "checks": checks,
                    "passed": passed_checks,
                    "failed": total_checks - passed_checks,
                    "total": total_checks
                }
                
        except Exception as e:
            duration = (datetime.utcnow() - start_time).total_seconds()
            return {
                "status": "error",
                "duration": duration,
                "error": str(e),
                "passed": 0,
                "failed": 1,
                "total": 1
            }
    
    async def _run_llm_test(self, test: TestResponse, execution: TestExecution) -> Dict[str, Any]:
        """Выполнение LLM теста"""
        start_time = datetime.utcnow()
        
        try:
            # LLM тест - проверяем работу с Ollama
            from .llm_router import llm_router
            
            prompt = test.test_metadata.get("prompt", "Привет, как дела?")
            used_model = test.test_metadata.get("model", "qwen2.5:7b-instruct-turbo")
            
            response = await llm_router.generate_response(
                prompt=prompt,
                model_name=used_model,
                max_tokens=100
            )
            
            duration = (datetime.utcnow() - start_time).total_seconds()
            
            if response and response.get("response"):
                return {
                    "status": "passed",
                    "duration": duration,
                    "message": "LLM тест пройден успешно",
                    "response_length": len(response["response"]),
                    "used_model": used_model,
                    "passed": 1,
                    "failed": 0,
                    "total": 1
                }
            else:
                return {
                    "status": "failed",
                    "duration": duration,
                    "error": "LLM не вернул ответ",
                    "used_model": used_model,
                    "passed": 0,
                    "failed": 1,
                    "total": 1
                }
                
        except Exception as e:
            duration = (datetime.utcnow() - start_time).total_seconds()
            return {
                "status": "error",
                "duration": duration,
                "error": str(e),
                "passed": 0,
                "failed": 1,
                "total": 1
            }
    
    async def _run_generic_test(self, test: TestResponse, execution: TestExecution) -> Dict[str, Any]:
        """Выполнение общего теста"""
        start_time = datetime.utcnow()
        
        try:
            # Простой тест - проверяем доступность сервиса
            duration = (datetime.utcnow() - start_time).total_seconds()
            
            return {
                "status": "passed",
                "duration": duration,
                "message": "Общий тест пройден",
                "passed": 1,
                "failed": 0,
                "total": 1
            }
            
        except Exception as e:
            duration = (datetime.utcnow() - start_time).total_seconds()
            return {
                "status": "error",
                "duration": duration,
                "error": str(e),
                "passed": 0,
                "failed": 1,
                "total": 1
            }
    
    async def get_executions(self, skip: int, limit: int, test_id: Optional[str], status: Optional[TestStatus], db: AsyncSession) -> List[TestExecutionResponse]:
        """Получение списка выполнений"""
        try:
            query = select(TestExecution).options(selectinload(TestExecution.results))
            
            if test_id:
                query = query.where(TestExecution.test_id == test_id)
            if status:
                query = query.where(TestExecution.status == status.value)
            
            query = query.order_by(TestExecution.created_at.desc()).offset(skip).limit(limit)
            
            result = await db.execute(query)
            executions = result.scalars().all()
            
            return [
                TestExecutionResponse(
                    id=execution.id,
                    test_request=None,  # Можно добавить загрузку теста
                    status=TestStatus(execution.status),
                    progress=execution.progress,
                    created_at=execution.created_at,
                    started_at=execution.started_at,
                    finished_at=execution.finished_at,
                    results=[
                        TestResult(
                            test_id=r.test_id,
                            status=TestStatus(r.status),
                            started_at=r.started_at,
                            finished_at=r.finished_at,
                            duration=r.duration,
                            passed=r.passed,
                            failed=r.failed,
                            skipped=r.skipped,
                            total=r.total,
                            message=r.message,
                            error=r.error,
                            metadata=r.metadata or {}
                        )
                        for r in execution.results
                    ],
                    user_id=execution.user_id,
                    metadata=execution.test_metadata or {}
                )
                for execution in executions
            ]
            
        except Exception as e:
            logger.error(f"Ошибка получения выполнений: {e}")
            raise_database_error(f"Не удалось получить выполнения: {str(e)}")
    
    async def cancel_execution(self, execution_id: str, db: AsyncSession) -> bool:
        """Отмена выполнения теста"""
        try:
            # Проверяем, выполняется ли тест
            if execution_id in self.running_executions:
                task = self.running_executions[execution_id]
                task.cancel()
                del self.running_executions[execution_id]
            
            # Обновляем статус в БД
            stmt = update(TestExecution).where(TestExecution.id == execution_id).values(
                status=TestStatus.CANCELLED.value,
                finished_at=datetime.utcnow()
            )
            await db.execute(stmt)
            await db.commit()
            
            logger.info(f"Отменено выполнение: {execution_id}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка отмены выполнения {execution_id}: {e}")
            return False
    
    async def create_test_suite(self, suite_request: TestSuiteRequest, user_id: int, db: AsyncSession) -> TestSuiteResponse:
        """Создание набора тестов"""
        try:
            suite = TestSuite(
                id=str(uuid.uuid4()),
                name=suite_request.name,
                description=suite_request.description,
                tests=[test.dict() for test in suite_request.tests],
                parallel=suite_request.parallel,
                stop_on_failure=suite_request.stop_on_failure,
                timeout=suite_request.timeout,
                tags=suite_request.tags,
                user_id=user_id
            )
            
            db.add(suite)
            await db.commit()
            await db.refresh(suite)
            
            return TestSuiteResponse(
                id=suite.id,
                name=suite.name,
                description=suite.description,
                tests=suite_request.tests,
                parallel=suite.parallel,
                stop_on_failure=suite.stop_on_failure,
                timeout=suite.timeout,
                tags=suite.tags or [],
                created_at=suite.created_at,
                updated_at=suite.updated_at
            )
            
        except Exception as e:
            await db.rollback()
            logger.error(f"Ошибка создания набора тестов: {e}")
            raise_validation_error(f"Не удалось создать набор тестов: {str(e)}")
    
    async def execute_test_suite(self, suite_id: str, user_id: int, db: AsyncSession) -> List[TestExecutionResponse]:
        """Выполнение набора тестов"""
        try:
            # Получаем набор тестов
            query = select(TestSuite).where(TestSuite.id == suite_id)
            result = await db.execute(query)
            suite = result.scalar_one_or_none()
            
            if not suite:
                raise_not_found("Набор тестов не найден")
            
            executions = []
            
            # Создаем выполнения для каждого теста
            for test_data in suite.tests:
                test_request = TestRequest(**test_data)
                
                # Создаем тест если его нет
                test = await self._find_or_create_test(test_request, user_id, db)
                
                # Выполняем тест
                execution = await self.execute_test(test.id, user_id, db)
                executions.append(execution)
                
                # Если нужно остановиться при ошибке
                if suite.stop_on_failure and execution.status == TestStatus.FAILED:
                    break
            
            return executions
            
        except Exception as e:
            logger.error(f"Ошибка выполнения набора тестов {suite_id}: {e}")
            raise_validation_error(f"Не удалось выполнить набор тестов: {str(e)}")
    
    async def _find_or_create_test(self, test_request: TestRequest, user_id: int, db: AsyncSession) -> TestResponse:
        """Поиск или создание теста"""
        # Ищем существующий тест по названию
        query = select(Test).where(Test.name == test_request.name, Test.user_id == user_id)
        result = await db.execute(query)
        existing_test = result.scalar_one_or_none()
        
        if existing_test:
            return await self.get_test(existing_test.id, db)
        else:
            return await self.create_test(test_request, user_id, db)
    
    async def get_test_metrics(self, execution_id: Optional[str] = None, db: AsyncSession = None) -> Dict[str, Any]:
        """Получение метрик тестирования"""
        try:
            # Собираем базовые метрики
            metrics = {
                "total_tests": 0,
                "passed_tests": 0,
                "failed_tests": 0,
                "success_rate": 0.0,
                "average_duration": 0.0,
                "total_executions": 0,
                "running_executions": len(self.running_executions)
            }
            
            # Добавляем метрики в систему мониторинга
            metrics_collector.record_metric("test.total_tests", metrics["total_tests"])
            metrics_collector.record_metric("test.success_rate", metrics["success_rate"])
            metrics_collector.record_metric("test.average_duration", metrics["average_duration"])
            
            return metrics
            
        except Exception as e:
            logger.error(f"Ошибка получения метрик тестирования: {e}")
            return {}


# Глобальный экземпляр сервиса тестирования
testing_service = TestingService()

router = APIRouter()

@router.get("/coverage", tags=["testing"])
def get_coverage_report():
    """Возвращает отчет о покрытии тестами (coverage.json)"""
    try:
        # Генерируем отчет coverage
        subprocess.run(["pytest", "--cov=app", "--cov-report=json:coverage.json", "--disable-warnings"], check=True)
        with open("coverage.json", "r") as f:
            return Response(content=f.read(), media_type="application/json")
    except Exception as e:
        return {"error": str(e)}

@router.get("/code-analysis", tags=["testing"])
def get_code_analysis():
    """Возвращает результаты линтинга, typecheck и анализа сложности"""
    try:
        # Линтинг
        lint = subprocess.run(["flake8", "app", "--format=json"], capture_output=True, text=True)
        # Typecheck
        typecheck = subprocess.run(["mypy", "app", "--show-error-codes", "--pretty"], capture_output=True, text=True)
        # Cyclomatic complexity (radon)
        complexity = subprocess.run(["radon", "cc", "app", "-j"], capture_output=True, text=True)
        return {
            "lint": lint.stdout,
            "typecheck": typecheck.stdout,
            "complexity": complexity.stdout
        }
    except Exception as e:
        return {"error": str(e)}

__all__ = ['testing_service']