"""
광덕고 급식 알리미 & 평가 시스템 (웹 버전)
- Flask 웹사이트: 급식 보기, 메뉴별 별점 평가, 만족도 통계
- 평가 저장: DATABASE_URL 환경 변수가 있으면 PostgreSQL(Render/Neon), 없으면 내 컴퓨터의 ratings.db(SQLite)
- 나이스 인증키: NEIS_API_KEY 환경 변수, 없으면 같은 폴더의 key.txt 파일에서 읽음
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, redirect, render_template, request, url_for

# ===== 설정 =====
BASE_URL = "https://open.neis.go.kr/hub"
OFFICE_CODE = "F10"          # 전남광주통합특별시교육청
SCHOOL_CODE = "7140328"      # 광주 광덕고등학교
KST = timezone(timedelta(hours=9))   # 서버가 외국에 있어도 한국 날짜로 계산
MIN_COUNT = 5                # 순위에 넣을 메뉴의 최소 평가 수
MAX_COMMENT = 30             # 한 줄 평 최대 글자 수
BAD_WORDS = ["바보", "멍청", "시발", "씨발", "병신", "개새"]
DATABASE_URL = os.environ.get("DATABASE_URL", "")

app = Flask(__name__)


def get_api_key():
    key = os.environ.get("NEIS_API_KEY", "")
    if not key and os.path.exists("key.txt"):
        with open("key.txt", encoding="utf-8") as f:
            key = f.read()
    return key.strip()


def today():
    return datetime.now(KST).strftime("%Y%m%d")


# ===== 급식 데이터 =====
meal_cache = {}   # 같은 날짜를 여러 번 요청하지 않도록 저장


def clean_menu(text):
    """'돼지갈비매운찜(고등) (5.6.10.13)<br/>배추김치 (9)' → ['돼지갈비매운찜', '배추김치']"""
    menus = []
    for item in text.split("<br/>"):
        item = re.sub(r"\(.*?\)", "", item).replace("*", "").strip()
        if item:
            menus.append(item)
    return menus


def get_meal(date):
    """{'중식': {'메뉴': [...], '칼로리': '...'}, '석식': {...}} / 급식 없으면 {} / 오류면 None"""
    if date in meal_cache:
        return meal_cache[date]
    params = {
        "KEY": get_api_key(), "Type": "json",
        "ATPT_OFCDC_SC_CODE": OFFICE_CODE, "SD_SCHUL_CODE": SCHOOL_CODE,
        "MLSV_YMD": date,
    }
    try:
        data = requests.get(f"{BASE_URL}/mealServiceDietInfo", params=params, timeout=10).json()
    except (requests.RequestException, ValueError):
        return None

    result = {}
    if "mealServiceDietInfo" in data:
        for row in data["mealServiceDietInfo"][1]["row"]:
            result[row["MMEAL_SC_NM"]] = {
                "메뉴": clean_menu(row["DDISH_NM"]),
                "칼로리": row.get("CAL_INFO", ""),
            }
    elif data.get("RESULT", {}).get("CODE") != "INFO-200":   # INFO-200 = 그날 급식 없음
        return None
    meal_cache[date] = result
    return result


def date_label(date):
    d = datetime.strptime(date, "%Y%m%d")
    return f"{d.month}월 {d.day}일 {'월화수목금토일'[d.weekday()]}요일"


# ===== 평가 저장소 (데이터베이스) =====
def connect():
    if DATABASE_URL:
        import psycopg   # Render/Neon에서만 필요
        return psycopg.connect(DATABASE_URL)
    return sqlite3.connect("ratings.db")


def run(sql, params=(), fetch=False):
    """SQL 실행. SQLite는 ?, PostgreSQL은 %s를 쓰므로 자동으로 바꿔 준다."""
    if DATABASE_URL:
        sql = sql.replace("?", "%s")
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def init_db():
    id_col = "SERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"
    run(f"""CREATE TABLE IF NOT EXISTS ratings (
        id {id_col},
        date TEXT, meal TEXT, sid TEXT, menu TEXT, score INTEGER, comment TEXT
    )""")


def already_rated(date, sid, meal):
    rows = run("SELECT 1 FROM ratings WHERE date=? AND sid=? AND meal=? LIMIT 1",
               (date, sid, meal), fetch=True)
    return bool(rows)


def save_ratings(date, meal, sid, scores, comment):
    first = True
    for menu, score in scores.items():
        run("INSERT INTO ratings (date, meal, sid, menu, score, comment) VALUES (?, ?, ?, ?, ?, ?)",
            (date, meal, sid, menu, score, comment if first else ""))
        first = False


# ===== 화면 1: 홈 =====
def shift(date, days):
    """날짜(YYYYMMDD)를 days일 앞뒤로 옮긴다."""
    return (datetime.strptime(date, "%Y%m%d") + timedelta(days=days)).strftime("%Y%m%d")


@app.route("/")
def index():
    date = request.args.get("date", "").replace("-", "")   # 달력에서 고르면 2026-11-16 형태
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError:
        date = today()                                       # 날짜가 없거나 틀리면 오늘
    meal = get_meal(date)
    tab = request.args.get("meal", "중식")
    if meal and tab not in meal:
        tab = next(iter(meal))
    return render_template(
        "index.html", date=date, date_label=date_label(date), meal=meal, tab=tab,
        is_today=(date == today()), prev_date=shift(date, -1), next_date=shift(date, 1),
        date_value=f"{date[:4]}-{date[4:6]}-{date[6:]}",
    )


# ===== 화면 2: 평가하기 =====
@app.route("/rate", methods=["GET", "POST"])
def rate():
    date = today()
    meal = get_meal(date)
    tab = request.values.get("meal", "중식")
    if meal and tab not in meal:
        tab = next(iter(meal))
    errors, form = {}, {"sid": "", "comment": "", "scores": {}}

    if request.method == "POST" and meal:
        sid = request.form.get("sid", "").strip()
        comment = request.form.get("comment", "").strip()
        scores = {}
        for i, menu in enumerate(meal[tab]["메뉴"]):
            value = request.form.get(f"score{i}", "")
            if value in {"1", "2", "3", "4", "5"}:
                scores[menu] = int(value)
        form = {"sid": sid, "comment": comment, "scores": scores}

        # 입력값 검사
        if not (len(sid) == 5 and sid.isdigit()):
            errors["sid"] = "학번은 5자리 숫자로 입력해 주세요"
        if len(comment) > MAX_COMMENT:
            errors["comment"] = f"한 줄 평은 {MAX_COMMENT}자 이내로 입력해 주세요 (현재 {len(comment)}자)"
        elif any(w in comment for w in BAD_WORDS):
            errors["comment"] = "바른 말로 다시 입력해 주세요"
        if not scores:
            errors["scores"] = "별점을 하나 이상 눌러 주세요"
        if not errors and already_rated(date, sid, tab):
            errors["sid"] = f"오늘 {tab}은 이미 평가했어요"

        # 저장
        if not errors:
            save_ratings(date, tab, sid, scores, comment)
            avg = sum(scores.values()) / len(scores)
            return redirect(url_for("rate", meal=tab, saved=f"{avg:.1f}"))

    return render_template("rate.html", meal=meal, tab=tab, errors=errors, form=form,
                           saved=request.args.get("saved"), max_comment=MAX_COMMENT)


# ===== 화면 3: 통계 =====
@app.route("/stats")
def stats():
    rows = run("SELECT meal, menu, score, comment, date FROM ratings ORDER BY id", fetch=True)

    by_menu, by_meal = {}, {"중식": [], "석식": []}
    for meal, menu, score, comment, date in rows:
        by_menu.setdefault(menu, []).append(score)
        by_meal.setdefault(meal, []).append(score)

    ranking = []
    for menu, scores in by_menu.items():
        if len(scores) >= MIN_COUNT:
            avg = sum(scores) / len(scores)
            ranking.append({"menu": menu, "avg": avg, "count": len(scores),
                            "width": round(avg / 5 * 100)})
    ranking.sort(key=lambda r: r["avg"], reverse=True)

    averages = {}
    for meal, scores in by_meal.items():
        averages[meal] = {"avg": sum(scores) / len(scores) if scores else None, "count": len(scores)}

    comments = [{"text": c, "meal": m, "date": f"{d[4:6]}/{d[6:]}"}
                for m, _, _, c, d in rows if c][-5:][::-1]

    return render_template("stats.html", top=ranking[:5], worst=ranking[::-1][:3] if len(ranking) > 5 else [],
                           averages=averages, comments=comments, min_count=MIN_COUNT)


init_db()

if __name__ == "__main__":
    app.run(debug=True)
