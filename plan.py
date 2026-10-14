# -*- coding: utf-8 -*-
"""学习计划与打卡（plan.json）。

数据结构：
{
  "dailyGoalMin": 20,                       # 每日目标分钟数
  "plans": {
    "2026-10-08": {                         # 按日期一天一份
      "createdAt": "...", "source": "llm|local",
      "tasks": [{"id","text","type","done","doneAt"}]
    }
  },
  "lastRemindDate": "2026-10-08"            # 启动提醒去重
}

打卡判定：某天「有练习记录（practice.json 由调用方传入）或任务全部完成」即算打卡。
"""
import json
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

_LOCK = threading.RLock()


def _today():
    return datetime.now().strftime('%Y-%m-%d')


def _yesterday():
    return (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')


class Plan:
    def __init__(self, path, practice_file=None):
        self.path = Path(path)
        self.practice_file = Path(practice_file) if practice_file else None
        self.data = {'dailyGoalMin': 20, 'plans': {}, 'lastRemindDate': ''}
        self._load()

    # ---------- 基础读写
    def _load(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(d, dict):
                    self.data.update(d)
        except Exception:
            pass
        self.data.setdefault('dailyGoalMin', 20)
        self.data.setdefault('plans', {})
        self.data.setdefault('lastRemindDate', '')

    def _save(self):
        try:
            import store
            store.write_json(self.path, self.data)
        except Exception:
            pass

    def _day(self, ds):
        return self.data['plans'].setdefault(
            ds, {'createdAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 'source': 'local', 'tasks': []})

    # ---------- 对外 API
    def today(self):
        """今日计划 + 统计摘要（给 UI 用）。"""
        with _LOCK:
            self._load()
            ds = _today()
            day = self.data['plans'].get(ds) or {}
            tasks = day.get('tasks') or []
            return {'ok': True, 'date': ds, 'dailyGoalMin': self.data['dailyGoalMin'],
                    'source': day.get('source') or '',
                    'tasks': tasks,
                    'doneCount': sum(1 for t in tasks if t.get('done')),
                    'totalCount': len(tasks),
                    'streak': self.streak()}

    def set_tasks(self, tasks, daily_goal_min=None, source='llm', date_str=None):
        """设置某天的任务清单（整体替换，LLM 生成 / 本地编辑共用）。"""
        with _LOCK:
            self._load()
            ds = (date_str or _today()).strip()
            clean = []
            for t in tasks or []:
                if isinstance(t, str):
                    t = {'text': t}
                if not isinstance(t, dict):
                    continue
                text = (t.get('text') or '').strip()
                if not text:
                    continue
                entry = {'id': t.get('id') or uuid.uuid4().hex[:8],
                         'text': text[:120],
                         'type': (t.get('type') or 'task')[:20],
                         'done': bool(t.get('done')),
                         'doneAt': t.get('doneAt') or ''}
                # 句子级任务：关联到具体语料的某句（可「▶ 去练习」直达）
                if t.get('item_id'):
                    entry['item_id'] = str(t['item_id'])[:120]
                    try:
                        entry['index'] = max(0, int(t.get('index') or 0))
                    except (TypeError, ValueError):
                        entry['index'] = 0
                clean.append(entry)
            if daily_goal_min:
                try:
                    self.data['dailyGoalMin'] = max(5, min(240, int(daily_goal_min)))
                except (TypeError, ValueError):
                    pass
            day = self._day(ds)
            if clean:
                old = {t['id']: t for t in day.get('tasks') or []}
                for t in clean:      # 保留已有任务的完成状态（按 id 对齐）
                    if t['id'] in old and old[t['id']].get('done'):
                        t['done'] = True
                        t['doneAt'] = old[t['id']].get('doneAt') or ''
                day['tasks'] = clean
            day['source'] = source
            self._save()
            return {'ok': True, 'date': ds, 'count': len(clean),
                    'dailyGoalMin': self.data['dailyGoalMin']}

    def add_task(self, text, ttype='task', date_str=None):
        """本地加一条任务（保留已有）。"""
        text = (text or '').strip()
        if not text:
            return {'ok': False, 'msg': '任务内容为空'}
        with _LOCK:
            self._load()
            day = self._day(date_str or _today())
            day['tasks'].append({'id': uuid.uuid4().hex[:8], 'text': text[:120],
                                 'type': ttype[:20], 'done': False, 'doneAt': ''})
            self._save()
            return {'ok': True, 'count': len(day['tasks'])}

    def mark_done(self, task_id, done=True):
        """打卡 / 取消打卡（task_id 或 'all'）。"""
        with _LOCK:
            self._load()
            ds = _today()
            day = self.data['plans'].get(ds)
            if not day:
                return {'ok': False, 'msg': '今天还没有计划任务'}
            now = datetime.now().strftime('%H:%M')
            hit = 0
            for t in day.get('tasks') or []:
                if task_id == 'all' or t.get('id') == task_id:
                    t['done'] = bool(done)
                    t['doneAt'] = now if done else ''
                    hit += 1
            if not hit:
                return {'ok': False, 'msg': '找不到该任务'}
            self._save()
            tasks = day.get('tasks') or []
            return {'ok': True, 'marked': hit,
                    'doneCount': sum(1 for t in tasks if t.get('done')),
                    'totalCount': len(tasks), 'streak': self.streak()}

    def _practiced_dates(self, days=400):
        """从 practice.json 取最近有练习记录的日期集合。"""
        out = set()
        if not self.practice_file or not self.practice_file.exists():
            return out
        try:
            items = json.loads(self.practice_file.read_text(encoding='utf-8'))
            cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            for x in items if isinstance(items, list) else []:
                at = (x.get('at') or '')[:10]
                if at >= cutoff:
                    out.add(at)
        except Exception:
            pass
        return out

    def day_done(self, ds, practiced):
        """某天是否算打卡：有练习记录，或当日任务存在且全部完成。"""
        if ds in practiced:
            return True
        tasks = (self.data['plans'].get(ds) or {}).get('tasks') or []
        return bool(tasks) and all(t.get('done') for t in tasks)

    def streak(self):
        """连续打卡天数：从今天（或昨天）往回数，今天还没打卡不打断连续。"""
        with _LOCK:
            practiced = self._practiced_dates()
            d = datetime.now()
            if not self.day_done(d.strftime('%Y-%m-%d'), practiced):
                d -= timedelta(days=1)          # 今天还没练不打断
            n = 0
            for _ in range(365):
                if self.day_done(d.strftime('%Y-%m-%d'), practiced):
                    n += 1
                    d -= timedelta(days=1)
                else:
                    break
            return n

    def heatmap(self, weeks=12):
        """近 N 周逐日打卡情况（数据中心热力图用）。返回旧的在前。"""
        with _LOCK:
            practiced = self._practiced_dates()
            days = []
            d = datetime.now().date()
            start = d - timedelta(days=min(weeks * 7, 364))
            while start <= d:
                ds = start.strftime('%Y-%m-%d')
                tasks = (self.data['plans'].get(ds) or {}).get('tasks') or []
                all_done = bool(tasks) and all(t.get('done') for t in tasks)
                days.append({'date': ds, 'practiced': ds in practiced,
                             'tasksAllDone': all_done})
                start += timedelta(days=1)
            return days

    def pending_yesterday(self):
        """昨天有任务但没全完成 → 返回未完成任务列表（用于启动提醒）。"""
        with _LOCK:
            self._load()
            ds = _yesterday()
            tasks = (self.data['plans'].get(ds) or {}).get('tasks') or []
            return [t for t in tasks if not t.get('done')]

    def mark_reminded(self):
        with _LOCK:
            self.data['lastRemindDate'] = _today()
            self._save()

    def should_remind(self):
        """今天还没提醒过、昨天有未完成任务、且昨天没练习 → 该提醒。"""
        with _LOCK:
            self._load()
            if self.data.get('lastRemindDate') == _today():
                return []
            practiced = self._practiced_dates()
            if _yesterday() in practiced:
                return []
            return self.pending_yesterday()

    def set_goal(self, minutes):
        try:
            m = max(5, min(240, int(minutes)))
        except (TypeError, ValueError):
            return {'ok': False, 'msg': '分钟数不合法'}
        with _LOCK:
            self._load()
            self.data['dailyGoalMin'] = m
            self._save()
        return {'ok': True, 'dailyGoalMin': m}
