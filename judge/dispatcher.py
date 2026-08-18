from utils.tutor_engine import CodingTutor
from submission.models import JudgeStatus, Submission
import ast as ast_module
import hashlib
import json
import logging
import subprocess
import tempfile
from urllib.parse import urljoin

import requests
from django.db import transaction, IntegrityError
from django.db.models import F

from account.models import User
from conf.models import JudgeServer
from contest.models import ContestRuleType, ACMContestRank, OIContestRank, ContestStatus
from options.options import SysOptions
from problem.models import Problem, ProblemRuleType
from problem.utils import parse_problem_template
from utils.cache import cache
from utils.constants import CacheKey

logger = logging.getLogger(__name__)


def classify_error_type(result, static_results):
    if result == JudgeStatus.COMPILE_ERROR:
        return "SYNTAX"
    elif result == JudgeStatus.WRONG_ANSWER:
        return "LOGIC"
    elif result in [JudgeStatus.RUNTIME_ERROR, JudgeStatus.CPU_TIME_LIMIT_EXCEEDED,
                    JudgeStatus.REAL_TIME_LIMIT_EXCEEDED, JudgeStatus.MEMORY_LIMIT_EXCEEDED]:
        return "RUNTIME"
    elif static_results:
        return "STYLE"
    return "UNKNOWN"


def run_static_analysis(code):
    static_analysis = []
    try:
        ast_module.parse(code)
    except SyntaxError as syn_err:
        static_analysis.append("[AST] 語法錯誤: 第 " + str(syn_err.lineno) + " 行 - " + str(syn_err.msg))
    try:
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            tmp_path = f.name
        result = subprocess.run(
            ["flake8", "--max-line-length=120", "--select=E,W,F", tmp_path],
            capture_output=True, text=True, timeout=10
        )
        if result.stdout:
            lines = result.stdout.strip().split("\n")[:5]
            for line in lines:
                msg = line.split(tmp_path)[-1].strip().lstrip(":")
                static_analysis.append("[flake8] " + msg)
    except Exception:
        pass
    return static_analysis


def process_pending_task():
    if cache.llen(CacheKey.waiting_queue):
        from judge.tasks import judge_task
        tmp_data = cache.rpop(CacheKey.waiting_queue)
        if tmp_data:
            data = json.loads(tmp_data.decode("utf-8"))
            judge_task.send(**data)


class ChooseJudgeServer:
    def __init__(self):
        self.server = None

    def __enter__(self) -> [JudgeServer, None]:
        with transaction.atomic():
            servers = JudgeServer.objects.select_for_update().filter(is_disabled=False).order_by("task_number")
            servers = [s for s in servers if s.status == "normal"]
            for server in servers:
                if server.task_number <= server.cpu_core * 2:
                    server.task_number = F("task_number") + 1
                    server.save(update_fields=["task_number"])
                    self.server = server
                    return server
        return None

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.server:
            JudgeServer.objects.filter(id=self.server.id).update(task_number=F("task_number") - 1)


class DispatcherBase(object):
    def __init__(self):
        self.token = hashlib.sha256(SysOptions.judge_server_token.encode("utf-8")).hexdigest()

    def _request(self, url, data=None):
        kwargs = {"headers": {"X-Judge-Server-Token": self.token}}
        if data:
            kwargs["json"] = data
        try:
            return requests.post(url, **kwargs).json()
        except Exception as e:
            logger.exception(e)


class SPJCompiler(DispatcherBase):
    def __init__(self, spj_code, spj_version, spj_language):
        super().__init__()
        spj_compile_config = list(filter(lambda config: spj_language == config["name"], SysOptions.spj_languages))[0]["spj"]["compile"]
        self.data = {
            "src": spj_code,
            "spj_version": spj_version,
            "spj_compile_config": spj_compile_config
        }

    def compile_spj(self):
        with ChooseJudgeServer() as server:
            if not server:
                return "No available judge_server"
            result = self._request(urljoin(server.service_url, "compile_spj"), data=self.data)
            if not result:
                return "Failed to call judge server"
            if result["err"]:
                return result["data"]


class JudgeDispatcher(DispatcherBase):
    def __init__(self, submission_id, problem_id):
        super().__init__()
        self.submission = Submission.objects.get(id=submission_id)
        self.contest_id = self.submission.contest_id
        self.last_result = self.submission.result if self.submission.info else None

        if self.contest_id:
            self.problem = Problem.objects.select_related("contest").get(id=problem_id, contest_id=self.contest_id)
            self.contest = self.problem.contest
        else:
            self.problem = Problem.objects.get(id=problem_id)

    def _compute_statistic_info(self, resp_data):
        self.submission.statistic_info["time_cost"] = max([x["cpu_time"] for x in resp_data])
        self.submission.statistic_info["memory_cost"] = max([x["memory"] for x in resp_data])

        if self.problem.rule_type == ProblemRuleType.OI:
            score = 0
            try:
                for i in range(len(resp_data)):
                    if resp_data[i]["result"] == JudgeStatus.ACCEPTED:
                        resp_data[i]["score"] = self.problem.test_case_score[i]["score"]
                        score += resp_data[i]["score"]
                    else:
                        resp_data[i]["score"] = 0
            except IndexError:
                logger.error("Index Error raised when summing up the score in problem " + str(self.problem.id))
                self.submission.statistic_info["score"] = 0
                return
            self.submission.statistic_info["score"] = score

    def judge(self):
        language = self.submission.language
        sub_config = list(filter(lambda item: language == item["name"], SysOptions.languages))[0]
        spj_config = {}
        if self.problem.spj_code:
            for lang in SysOptions.spj_languages:
                if lang["name"] == self.problem.spj_language:
                    spj_config = lang["spj"]
                    break

        if language in self.problem.template:
            template = parse_problem_template(self.problem.template[language])
            code = template['prepend'] + "\n" + self.submission.code + "\n" + template['append']
        else:
            code = self.submission.code

        data = {
            "language_config": sub_config["config"],
            "src": code,
            "max_cpu_time": self.problem.time_limit,
            "max_memory": 1024 * 1024 * self.problem.memory_limit,
            "test_case_id": self.problem.test_case_id,
            "output": False,
            "spj_version": self.problem.spj_version,
            "spj_config": spj_config.get("config"),
            "spj_compile_config": spj_config.get("compile"),
            "spj_src": self.problem.spj_code,
            "io_mode": self.problem.io_mode
        }

        with ChooseJudgeServer() as server:
            if not server:
                data = {"submission_id": self.submission.id, "problem_id": self.problem.id}
                cache.lpush(CacheKey.waiting_queue, json.dumps(data))
                return
            Submission.objects.filter(id=self.submission.id).update(result=JudgeStatus.JUDGING)
            resp = self._request(urljoin(server.service_url, "/judge"), data=data)

        if not resp:
            Submission.objects.filter(id=self.submission.id).update(result=JudgeStatus.SYSTEM_ERROR)
            return

        if resp["err"]:
            self.submission.result = JudgeStatus.COMPILE_ERROR
            self.submission.statistic_info["err_info"] = resp["data"]
            self.submission.statistic_info["score"] = 0
        else:
            resp["data"].sort(key=lambda x: int(x["test_case"]))
            self.submission.info = resp
            self._compute_statistic_info(resp["data"])
            error_test_case = list(filter(lambda case: case["result"] != 0, resp["data"]))
            if not error_test_case:
                self.submission.result = JudgeStatus.ACCEPTED
            elif self.problem.rule_type == ProblemRuleType.ACM or len(error_test_case) == len(resp["data"]):
                self.submission.result = error_test_case[0]["result"]
            else:
                self.submission.result = JudgeStatus.PARTIALLY_ACCEPTED

        # ====== 🎯 智慧診斷模組（AI Hint）注入點 ======
        if self.submission.result != 0:
            try:
                if self.submission.result == -2:
                    error_log = str(self.submission.statistic_info.get("err_info", "未知編譯錯誤"))
                else:
                    error_log = str(self.submission.info)

                static_results = run_static_analysis(self.submission.code)
                if static_results:
                    error_log += "\n\n【靜態分析結果】\n" + "\n".join(static_results)
                    logger.info("靜態分析結果: " + str(static_results))
                else:
                    logger.info("靜態分析：無發現問題")

                self.submission.error_type = classify_error_type(self.submission.result, static_results)

                tags = self.problem.tags.all()
                self.submission.problem_tags = str([t.name for t in tags])

                attempt_count = Submission.objects.filter(
                    user_id=self.submission.user_id,
                    problem_id=self.submission.problem_id
                ).count()

                tutor = CodingTutor()
                ai_hint_text = tutor.get_scaffolding_hint(
                    student_code=self.submission.code,
                    error_msg=error_log,
                    attempt_count=attempt_count
                )

                ai_hint_text["error_log"] = error_log
                self.submission.ai_hint = str(ai_hint_text)
            except Exception as ai_err:
                logger.error("AI Hint 錯誤: " + str(ai_err))

        # ====== 知識點追蹤更新 ======
        try:
            from django.db import connection
            from django.utils import timezone
            tags = self.problem.tags.all()
            is_correct = 1 if self.submission.result == 0 else 0
            error_type = self.submission.error_type or "UNKNOWN"
            for tag in tags:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, correct_count, attempt_count, consecutive_correct, response_sequence "
                        "FROM student_knowledge_state "
                        "WHERE user_id = %s AND knowledge_tag = %s",
                        [self.submission.user_id, tag.name]
                    )
                    row = cursor.fetchone()
                    if row is None:
                        seq = str(is_correct)
                        cursor.execute(
                            "INSERT INTO student_knowledge_state "
                            "(user_id, knowledge_tag, attempt_count, correct_count, error_count, "
                            "last_error_type, last_attempt, mastery_level, response_sequence, "
                            "syntax_error_count, logic_error_count, runtime_error_count, "
                            "consecutive_correct, recurrence_count) "
                            "VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)",
                            [
                                self.submission.user_id, tag.name,
                                is_correct, 1 - is_correct, error_type,
                                timezone.now(), float(is_correct), seq,
                                1 if error_type == "SYNTAX" else 0,
                                1 if error_type == "LOGIC" else 0,
                                1 if error_type == "RUNTIME" else 0,
                                is_correct
                            ]
                        )
                    else:
                        row_id, correct_count, attempt_count, consecutive_correct, response_sequence = row
                        new_attempt = attempt_count + 1
                        new_correct = correct_count + is_correct
                        new_error = 1 - is_correct
                        new_mastery = float(new_correct) / new_attempt
                        if response_sequence:
                            new_seq = response_sequence + "," + str(is_correct)
                        else:
                            new_seq = str(is_correct)
                        new_consecutive = consecutive_correct + 1 if is_correct == 1 else 0
                        recurrence = 1 if is_correct == 0 and consecutive_correct > 0 else 0
                        cursor.execute(
                            "UPDATE student_knowledge_state SET "
                            "attempt_count = %s, correct_count = %s, "
                            "error_count = error_count + %s, "
                            "last_error_type = %s, last_attempt = %s, "
                            "mastery_level = %s, response_sequence = %s, "
                            "syntax_error_count = syntax_error_count + %s, "
                            "logic_error_count = logic_error_count + %s, "
                            "runtime_error_count = runtime_error_count + %s, "
                            "consecutive_correct = %s, "
                            "recurrence_count = recurrence_count + %s "
                            "WHERE id = %s",
                            [
                                new_attempt, new_correct,
                                new_error,
                                error_type, timezone.now(),
                                new_mastery, new_seq,
                                1 if error_type == "SYNTAX" else 0,
                                1 if error_type == "LOGIC" else 0,
                                1 if error_type == "RUNTIME" else 0,
                                new_consecutive,
                                recurrence,
                                row_id
                            ]
                        )
        except Exception as kt_err:
            import traceback
            logger.error("知識追蹤更新錯誤: " + str(kt_err))
            logger.error(traceback.format_exc())
        # ==========================="
        # =============================================

        self.submission.save()

        if self.contest_id:
            if self.contest.status != ContestStatus.CONTEST_UNDERWAY or \
                    User.objects.get(id=self.submission.user_id).is_contest_admin(self.contest):
                logger.info(
                    "Contest debug mode, id: " + str(self.contest_id) + ", submission id: " + self.submission.id)
                return
            with transaction.atomic():
                self.update_contest_problem_status()
                self.update_contest_rank()
        else:
            if self.last_result:
                self.update_problem_status_rejudge()
            else:
                self.update_problem_status()

        process_pending_task()

    def update_problem_status_rejudge(self):
        result = str(self.submission.result)
        problem_id = str(self.problem.id)
        with transaction.atomic():
            problem = Problem.objects.select_for_update().get(contest_id=self.contest_id, id=self.problem.id)
            if self.last_result != JudgeStatus.ACCEPTED and self.submission.result == JudgeStatus.ACCEPTED:
                problem.accepted_number += 1
            problem_info = problem.statistic_info
            problem_info[self.last_result] = problem_info.get(self.last_result, 1) - 1
            problem_info[result] = problem_info.get(result, 0) + 1
            problem.save(update_fields=["accepted_number", "statistic_info"])

            profile = User.objects.select_for_update().get(id=self.submission.user_id).userprofile
            if problem.rule_type == ProblemRuleType.ACM:
                acm_problems_status = profile.acm_problems_status.get("problems", {})
                if acm_problems_status[problem_id]["status"] != JudgeStatus.ACCEPTED:
                    acm_problems_status[problem_id]["status"] = self.submission.result
                    if self.submission.result == JudgeStatus.ACCEPTED:
                        profile.accepted_number += 1
                profile.acm_problems_status["problems"] = acm_problems_status
                profile.save(update_fields=["accepted_number", "acm_problems_status"])
            else:
                oi_problems_status = profile.oi_problems_status.get("problems", {})
                score = self.submission.statistic_info["score"]
                if oi_problems_status[problem_id]["status"] != JudgeStatus.ACCEPTED:
                    profile.add_score(this_time_score=score,
                                      last_time_score=oi_problems_status[problem_id]["score"])
                    oi_problems_status[problem_id]["score"] = score
                    oi_problems_status[problem_id]["status"] = self.submission.result
                    if self.submission.result == JudgeStatus.ACCEPTED:
                        profile.accepted_number += 1
                profile.oi_problems_status["problems"] = oi_problems_status
                profile.save(update_fields=["accepted_number", "oi_problems_status"])

    def update_problem_status(self):
        result = str(self.submission.result)
        problem_id = str(self.problem.id)
        with transaction.atomic():
            problem = Problem.objects.select_for_update().get(contest_id=self.contest_id, id=self.problem.id)
            problem.submission_number += 1
            if self.submission.result == JudgeStatus.ACCEPTED:
                problem.accepted_number += 1
            problem_info = problem.statistic_info
            problem_info[result] = problem_info.get(result, 0) + 1
            problem.save(update_fields=["accepted_number", "submission_number", "statistic_info"])

            user = User.objects.select_for_update().get(id=self.submission.user_id)
            user_profile = user.userprofile
            user_profile.submission_number += 1
            if problem.rule_type == ProblemRuleType.ACM:
                acm_problems_status = user_profile.acm_problems_status.get("problems", {})
                if problem_id not in acm_problems_status:
                    acm_problems_status[problem_id] = {"status": self.submission.result, "_id": self.problem._id}
                    if self.submission.result == JudgeStatus.ACCEPTED:
                        user_profile.accepted_number += 1
                elif acm_problems_status[problem_id]["status"] != JudgeStatus.ACCEPTED:
                    acm_problems_status[problem_id]["status"] = self.submission.result
                    if self.submission.result == JudgeStatus.ACCEPTED:
                        user_profile.accepted_number += 1
                user_profile.acm_problems_status["problems"] = acm_problems_status
                user_profile.save(update_fields=["submission_number", "accepted_number", "acm_problems_status"])
            else:
                oi_problems_status = user_profile.oi_problems_status.get("problems", {})
                score = self.submission.statistic_info["score"]
                if problem_id not in oi_problems_status:
                    user_profile.add_score(score)
                    oi_problems_status[problem_id] = {"status": self.submission.result,
                                                      "_id": self.problem._id,
                                                      "score": score}
                    if self.submission.result == JudgeStatus.ACCEPTED:
                        user_profile.accepted_number += 1
                elif oi_problems_status[problem_id]["status"] != JudgeStatus.ACCEPTED:
                    user_profile.add_score(this_time_score=score,
                                           last_time_score=oi_problems_status[problem_id]["score"])
                    oi_problems_status[problem_id]["score"] = score
                    oi_problems_status[problem_id]["status"] = self.submission.result
                    if self.submission.result == JudgeStatus.ACCEPTED:
                        user_profile.accepted_number += 1
                user_profile.oi_problems_status["problems"] = oi_problems_status
                user_profile.save(update_fields=["submission_number", "accepted_number", "oi_problems_status"])

    def update_contest_problem_status(self):
        with transaction.atomic():
            user = User.objects.select_for_update().get(id=self.submission.user_id)
            user_profile = user.userprofile
            problem_id = str(self.problem.id)
            if self.contest.rule_type == ContestRuleType.ACM:
                contest_problems_status = user_profile.acm_problems_status.get("contest_problems", {})
                if problem_id not in contest_problems_status:
                    contest_problems_status[problem_id] = {"status": self.submission.result, "_id": self.problem._id}
                elif contest_problems_status[problem_id]["status"] != JudgeStatus.ACCEPTED:
                    contest_problems_status[problem_id]["status"] = self.submission.result
                else:
                    return
                user_profile.acm_problems_status["contest_problems"] = contest_problems_status
                user_profile.save(update_fields=["acm_problems_status"])
            elif self.contest.rule_type == ContestRuleType.OI:
                contest_problems_status = user_profile.oi_problems_status.get("contest_problems", {})
                score = self.submission.statistic_info["score"]
                if problem_id not in contest_problems_status:
                    contest_problems_status[problem_id] = {"status": self.submission.result,
                                                           "_id": self.problem._id,
                                                           "score": score}
                else:
                    contest_problems_status[problem_id]["score"] = score
                    contest_problems_status[problem_id]["status"] = self.submission.result
                user_profile.oi_problems_status["contest_problems"] = contest_problems_status
                user_profile.save(update_fields=["oi_problems_status"])

            problem = Problem.objects.select_for_update().get(contest_id=self.contest_id, id=self.problem.id)
            result = str(self.submission.result)
            problem_info = problem.statistic_info
            problem_info[result] = problem_info.get(result, 0) + 1
            problem.submission_number += 1
            if self.submission.result == JudgeStatus.ACCEPTED:
                problem.accepted_number += 1
            problem.save(update_fields=["submission_number", "accepted_number", "statistic_info"])

    def update_contest_rank(self):
        if self.contest.rule_type == ContestRuleType.OI or self.contest.real_time_rank:
            cache.delete(CacheKey.contest_rank_cache + ":" + str(self.contest.id))

        def get_rank(model):
            return model.objects.select_for_update().get(user_id=self.submission.user_id, contest=self.contest)

        if self.contest.rule_type == ContestRuleType.ACM:
            model = ACMContestRank
            func = self._update_acm_contest_rank
        else:
            model = OIContestRank
            func = self._update_oi_contest_rank

        try:
            rank = get_rank(model)
        except model.DoesNotExist:
            try:
                model.objects.create(user_id=self.submission.user_id, contest=self.contest)
                rank = get_rank(model)
            except IntegrityError:
                rank = get_rank(model)
        func(rank)

    def _update_acm_contest_rank(self, rank):
        info = rank.submission_info.get(str(self.submission.problem_id))
        problem = Problem.objects.select_for_update().get(contest_id=self.contest_id, id=self.problem.id)
        if info:
            if info["is_ac"]:
                return
            rank.submission_number += 1
            if self.submission.result == JudgeStatus.ACCEPTED:
                rank.accepted_number += 1
                info["is_ac"] = True
                info["ac_time"] = (self.submission.create_time - self.contest.start_time).total_seconds()
                rank.total_time += info["ac_time"] + info["error_number"] * 20 * 60
                if problem.accepted_number == 1:
                    info["is_first_ac"] = True
            elif self.submission.result != JudgeStatus.COMPILE_ERROR:
                info["error_number"] += 1
        else:
            rank.submission_number += 1
            info = {"is_ac": False, "ac_time": 0, "error_number": 0, "is_first_ac": False}
            if self.submission.result == JudgeStatus.ACCEPTED:
                rank.accepted_number += 1
                info["is_ac"] = True
                info["ac_time"] = (self.submission.create_time - self.contest.start_time).total_seconds()
                rank.total_time += info["ac_time"]
                if problem.accepted_number == 1:
                    info["is_first_ac"] = True
            elif self.submission.result != JudgeStatus.COMPILE_ERROR:
                info["error_number"] = 1
        rank.submission_info[str(self.submission.problem_id)] = info
        rank.save()

    def _update_oi_contest_rank(self, rank):
        problem_id = str(self.submission.problem_id)
        current_score = self.submission.statistic_info["score"]
        last_score = rank.submission_info.get(problem_id)
        if last_score:
            rank.total_score = rank.total_score - last_score + current_score
        else:
            rank.total_score = rank.total_score + current_score
        rank.submission_info[problem_id] = current_score
        rank.save()
