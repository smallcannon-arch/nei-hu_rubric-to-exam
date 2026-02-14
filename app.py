import re
import io
import pandas as pd
import streamlit as st
from pypdf import PdfReader
from docx import Document

# =========================
# 0) 基本設定
# =========================
st.set_page_config(page_title="出題助手｜審核導引站", layout="wide")

SUBJECT_Q_TYPES = {
    "國語": ["國字注音", "造句", "單選題", "閱讀素養題", "句型變換", "簡答題"],
    "數學": ["應用計算題", "圖表分析題", "填充題", "單選題", "是非題"],
    "自然科學": ["實驗判讀題", "圖表分析題", "單選題", "是非題", "填充題", "配合題"],
    "社會": ["地圖判讀題", "情境案例分析", "單選題", "是非題", "配合題", "簡答題"],
    "英語": ["英語會話選擇", "詞彙搭配", "文意選填", "單選題", "閱讀理解"],
    "": ["單選題", "是非題", "填充題", "簡答題"],
}

# 你的 GPT 連結（貼上你分享的 GPT URL）
GPT_URL = "https://chat.openai.com/"

PHASE1_PROMPT_TEMPLATE = """你是「國小正式評量命題與試題審核」專用 AI。
任務：閱讀教材，整理【學習目標審核表】（僅輸出 Markdown 表格）。

審核鐵律：
1. 配分總和必須剛好 100 分（整數）。
2. 「對應題型」只能填一種（禁止：A、B / A或B）。
3. 「預計配分」只能填阿拉伯數字。
4. 不得自行新增教材未出現的學習目標（避免常識外加）。

【參數】
年級：{grade}
科目：{subject}
命題模式：{mode}
可用題型：{types}

【教材】
{content}

【表格欄位（至少包含）】
| 單元 | 學習目標 | 對應題型 | 預計配分 |
"""

PHASE3_PROMPT_TEMPLATE = """你是「國小正式評量命題」專用 AI。
任務：依據【審核通過的審核表】正式出題。

命題鐵律：
- 題目數量與配分需與審核表一致，總分必須 100。
- 需要圖片請在題幹插入 [圖] 標籤（黑白印刷、線條清楚、繁中標示可留空格）。
- 干擾選項要合理，禁止「以上皆是/非」。

【基本資訊】
年級：{grade}
科目：{subject}
命題模式：{mode}

【審核表（請完全遵守）】
{review_table_md}

【輸出】
請直接輸出試卷：題號、題目、選項（如需要）、配分。
"""

# =========================
# 1) 檔案抽文字
# =========================
@st.cache_data
def extract_text(files):
    parts = []
    for f in files:
        ext = f.name.split(".")[-1].lower()
        text = ""
        if ext == "pdf":
            try:
                reader = PdfReader(f)
                for i, page in enumerate(reader.pages):
                    text += f"\n--- Page {i+1} ---\n" + (page.extract_text() or "")
                if not text.strip():
                    text = "(PDF 可能為純圖片或無可擷取文字)"
            except Exception:
                text = "(PDF 讀取失敗：可能加密或純圖片)"
        elif ext == "docx":
            try:
                doc = Document(f)
                text = "\n".join(p.text for p in doc.paragraphs)
            except Exception:
                text = "(DOCX 讀取失敗)"
        elif ext == "doc":
            text = "⚠️ 不支援 .doc，請另存為 .docx 或 .pdf 後重傳。"
        else:
            text = "(不支援的格式)"
        text = re.sub(r"\n\s*\n", "\n\n", text).strip()
        parts.append(f"=== 檔案：{f.name} ===\n{text}")
    return "\n\n".join(parts).strip()

# =========================
# 2) Markdown 表格 → DataFrame + 檢核
# =========================
def parse_md_table(md: str) -> pd.DataFrame | None:
    lines = [ln.strip() for ln in md.strip().splitlines() if "|" in ln]
    if len(lines) < 2:
        return None

    # 移除分隔線列
    def is_sep(ln):
        return bool(re.match(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?$", ln))
    lines = [ln for ln in lines if not is_sep(ln)]
    if len(lines) < 2:
        return None

    rows = [[c.strip() for c in ln.strip("|").split("|")] for ln in lines]
    headers = rows[0]
    body = rows[1:]

    max_cols = len(headers)
    fixed = []
    for r in body:
        if len(r) < max_cols:
            fixed.append(r + [""] * (max_cols - len(r)))
        else:
            fixed.append(r[:max_cols])

    df = pd.DataFrame(fixed, columns=headers)
    return df

def enforce_rules(df: pd.DataFrame) -> pd.DataFrame:
    # 題型只留第一個
    type_col = next((c for c in df.columns if "題型" in c), None)
    if type_col:
        def clean_type(x):
            t = str(x).replace(" ", "")
            for sep in ["、", ",", "或"]:
                if sep in t:
                    return t.split(sep)[0]
            return t
        df[type_col] = df[type_col].apply(clean_type)

    # 配分轉數字 + 校正 100
    score_col = next((c for c in df.columns if "配分" in c), None)
    if score_col:
        def to_num(x):
            nums = re.findall(r"[-+]?\d*\.\d+|\d+", str(x))
            return float(nums[0]) if nums else 0.0

        df[score_col] = df[score_col].apply(to_num)

        total = df[score_col].sum()
        if total > 0 and total != 100:
            df[score_col] = (df[score_col] / total) * 100

        df[score_col] = df[score_col].round().astype(int)
        diff = 100 - int(df[score_col].sum())
        if diff != 0:
            idx = df[score_col].idxmax()
            df.loc[idx, score_col] += diff

    return df

def df_to_excel_bytes(df: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="學習目標審核表")
        wb = writer.book
        ws = writer.sheets["學習目標審核表"]

        header = wb.add_format({"bold": True, "fg_color": "#D7E4BC", "border": 1, "valign": "vcenter"})
        wrap = wb.add_format({"text_wrap": True, "valign": "vcenter"})
        center = wb.add_format({"valign": "vcenter", "align": "center"})

        for c, name in enumerate(df.columns):
            ws.write(0, c, name, header)

        for i, col in enumerate(df.columns):
            if "目標" in col:
                ws.set_column(i, i, 60, wrap)
            elif "配分" in col:
                ws.set_column(i, i, 10, center)
            else:
                ws.set_column(i, i, 18, wrap)
    return out.getvalue()

# =========================
# 3) Session
# =========================
if "phase" not in st.session_state:
    st.session_state.phase = 1
if "content" not in st.session_state:
    st.session_state.content = ""
if "df" not in st.session_state:
    st.session_state.df = None
if "meta" not in st.session_state:
    st.session_state.meta = {}

# =========================
# 4) UI
# =========================
st.title("🌙 出題助手｜審核導引站（Copy/Paste 版）")
st.caption("網站負責把流程照亮；生成交給老師自己的 GPT 帳號。")

with st.sidebar:
    st.markdown("### 🚀 快速入口")
    st.markdown(f"- 開啟你的出題助手 GPT：{GPT_URL}")
    st.markdown("### 🔒 資料提醒")
    st.markdown("- 請勿上傳含學生姓名/學號/可辨識個資資料。")
    st.markdown("- 教材若受著作權保護，請確認校內使用授權範圍。")
    if st.button("🔄 重置流程"):
        st.session_state.clear()
        st.rerun()

# -------- Phase 1 --------
if st.session_state.phase == 1:
    st.subheader("Phase 1｜上傳教材 → 產生『審核表 Prompt』")

    c1, c2, c3 = st.columns(3)
    with c1:
        grade = st.selectbox("年級", ["", "一年級", "二年級", "三年級", "四年級", "五年級", "六年級"], index=0)
    with c2:
        subject = st.selectbox("科目", ["", "國語", "數學", "自然科學", "社會", "英語"], index=0)
    with c3:
        mode = st.selectbox("命題模式", ["🟢 模式 A：適中", "🔴 模式 B：困難", "🌟 模式 C：素養"], index=0)

    st.markdown("**可用題型（會放進 Prompt）**")
    types = SUBJECT_Q_TYPES.get(subject, SUBJECT_Q_TYPES[""])
    cols = st.columns(min(4, max(1, len(types))))
    selected = []
    for i, t in enumerate(types):
        if cols[i % len(cols)].checkbox(t, value=True):
            selected.append(t)

    files = st.file_uploader("上傳教材（PDF/DOCX）", type=["pdf", "docx", "doc"], accept_multiple_files=True)

    if st.button("🧾 擷取教材文字", type="primary", use_container_width=True):
        if not files:
            st.warning("先上傳教材檔案。")
        else:
            st.session_state.content = extract_text(files)
            st.toast("已擷取教材文字 ✅", icon="📄")

    if st.session_state.content:
        st.markdown("**教材文字預覽（可微調後再送去 GPT）**")
        edited_content = st.text_area("教材內容", st.session_state.content, height=240)
        st.session_state.content = edited_content

        if st.button("✨ 生成 Phase 1 Prompt（貼去 GPT）", use_container_width=True):
            if not grade or not subject or not mode or not selected:
                st.warning("請把年級/科目/模式/題型選好。")
            else:
                st.session_state.meta = {
                    "grade": grade,
                    "subject": subject,
                    "mode": mode,
                    "types": "、".join(selected),
                }
                st.session_state.phase = 1.5
                st.rerun()

# Phase 1.5（顯示 Prompt）
if st.session_state.phase == 1.5:
    meta = st.session_state.meta
    prompt = PHASE1_PROMPT_TEMPLATE.format(
        grade=meta["grade"],
        subject=meta["subject"],
        mode=meta["mode"],
        types=meta["types"],
        content=st.session_state.content,
    )

    st.subheader("Phase 1 Prompt｜複製後貼到你的 GPT")
    st.text_area("Prompt", prompt, height=320)
    st.download_button("⬇️ 下載 Prompt（.txt）", prompt.encode("utf-8"), "phase1_prompt.txt", use_container_width=True)

    st.info("把 GPT 回傳的『Markdown 表格』貼到下一步。")
    if st.button("➡️ 我已拿到審核表，進入 Phase 2", type="primary", use_container_width=True):
        st.session_state.phase = 2
        st.rerun()

# -------- Phase 2 --------
elif st.session_state.phase == 2:
    st.subheader("Phase 2｜貼回審核表 → 自動解析/檢核 → 下載 Excel")

    md = st.text_area("貼上 GPT 回傳的 Markdown 表格", height=220, placeholder="把 | 單元 | 學習目標 | ... 這種表格整段貼進來")
    colA, colB = st.columns(2)

    with colA:
        if st.button("📥 解析成表格", type="primary", use_container_width=True):
            df = parse_md_table(md)
            if df is None:
                st.error("看起來不像 Markdown 表格；請確認你貼的是『含 | 的表格』。")
            else:
                st.session_state.df = enforce_rules(df)
                st.toast("解析完成 ✅ 已套用題型單選與配分校正", icon="✅")

    if st.session_state.df is not None:
        df = st.session_state.df
        edited = st.data_editor(df, use_container_width=True, hide_index=True, num_rows="dynamic")
        edited = enforce_rules(edited)
        st.session_state.df = edited

        score_col = next((c for c in edited.columns if "配分" in c), None)
        total = int(edited[score_col].sum()) if score_col else 0
        if total != 100:
            st.warning(f"目前總分：{total}（建議調整為 100；系統會自動校正，但你也可以手動微調更貼近教學比重）")
        else:
            st.success("總分已對齊：100 ✅")

        excel_bytes = df_to_excel_bytes(edited)
        c1, c2, c3 = st.columns(3)
        with c1:
            st.download_button("📘 下載 Excel 審核表", excel_bytes, "審核表.xlsx", use_container_width=True)
        with c2:
            if st.button("⬅️ 回到 Phase 1", use_container_width=True):
                st.session_state.phase = 1
                st.rerun()
        with c3:
            if st.button("➡️ 生成 Phase 3 出題 Prompt", type="primary", use_container_width=True):
                st.session_state.phase = 3
                st.rerun()

# -------- Phase 3 --------
elif st.session_state.phase == 3:
    st.subheader("Phase 3｜產生『出題 Prompt』→ 貼去 GPT → 下載試卷")

    meta = st.session_state.meta
    df = st.session_state.df
    if df is None:
        st.error("找不到審核表資料，請回 Phase 2 重新貼入。")
        st.stop()

    review_md = df.to_markdown(index=False)
    prompt = PHASE3_PROMPT_TEMPLATE.format(
        grade=meta["grade"],
        subject=meta["subject"],
        mode=meta["mode"],
        review_table_md=review_md,
    )

    st.text_area("Phase 3 Prompt（貼去 GPT）", prompt, height=320)
    st.download_button("⬇️ 下載 Prompt（.txt）", prompt.encode("utf-8"), "phase3_prompt.txt", use_container_width=True)

    st.divider()
    st.markdown("**把 GPT 產出的試卷貼回來（方便集中下載/留存）**")
    exam = st.text_area("試卷內容", height=260, placeholder="把試卷整段貼進來")
    if exam.strip():
        st.download_button("📄 下載試卷（.txt）", exam.encode("utf-8"), "試卷.txt", use_container_width=True)
