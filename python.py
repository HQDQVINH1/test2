# app.py

import io
import json
import numpy as np
import pandas as pd
import streamlit as st
from google import genai
from google.genai.errors import APIError
from docx import Document

# ============== CẤU HÌNH ==============
st.set_page_config(page_title="Đánh giá Phương án Kinh doanh (DOCX) 📄➡️📊", layout="wide")
st.title("Đánh giá Phương án Kinh doanh từ file Word 📄➡️📊")
st.caption(
    "Upload .docx → **Lọc dữ liệu với AI** → kiểm tra/chỉnh form → **Tạo bảng dòng tiền & Tính chỉ số** → **🧠 AI phân tích**."
)

# ============== SESSION STATE ==============
# Kết quả tính toán (để AI phân tích)
if "analysis_ctx" not in st.session_state:
    st.session_state.analysis_ctx = None

# Dữ liệu form đang chỉnh (giữ qua rerun)
if "form_vals" not in st.session_state:
    st.session_state.form_vals = {
        "investment": 0.0,
        "lifetime_years": 5,
        "revenue_per_year": 0.0,
        "cost_per_year": 0.0,
        "wacc": 0.13,
        "tax_rate": 0.20,
    }

# Bộ đệm nhận từ AI extraction, sẽ hợp nhất vào form_vals trước khi render form
if "pending_extract" not in st.session_state:
    st.session_state.pending_extract = None

# ============== TIỆN ÍCH ==============
def read_docx_text(file_bytes: bytes) -> str:
    doc = Document(io.BytesIO(file_bytes))
    paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    return "\n".join(paras)

def safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        s2 = s.strip()
        if s2.startswith("```"):
            s2 = s2.strip("`")
            if s2.startswith("json"):
                s2 = s2[4:]
        s2 = s2.strip()
        if s2 and s2[-1] != "}":
            s2 += "}"
        try:
            return json.loads(s2)
        except Exception:
            return None

def parse_numbers(d, key, default=None):
    try:
        val = d.get(key, None)
        if val is None or (isinstance(val, str) and not val.strip()):
            return default
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).replace(",", "").replace(" ", "")
        if s.endswith("%"):
            return float(s[:-1]) / 100.0
        return float(s)
    except Exception:
        return default

def irr_bisection(cashflows, tol=1e-6, max_iter=100):
    def npv(r):
        return sum(cf / ((1 + r) ** t) for t, cf in enumerate(cashflows))
    low, high = -0.9999, 10.0
    f_low, f_high = npv(low), npv(high)
    tries = 0
    while f_low * f_high > 0 and tries < 5:
        high *= 2
        f_high = npv(high)
        tries += 1
    if f_low * f_high > 0:
        return None
    for _ in range(max_iter):
        mid = (low + high) / 2
        f_mid = npv(mid)
        if abs(f_mid) < tol:
            return mid
        if f_low * f_mid < 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return mid

def payback_period(cashflows):
    cum = 0.0
    for t, cf in enumerate(cashflows):
        cum += cf
        if cum >= 0:
            prev_cum = cum - cf
            need = -prev_cum
            frac = 0 if cf == 0 else need / cf
            return t - 1 + frac
    return None

def discounted_payback_period(cashflows, rate):
    cum = 0.0
    for t, cf in enumerate(cashflows):
        pv = cf / ((1 + rate) ** t)
        cum += pv
        if cum >= 0:
            prev_cum = cum - pv
            need = -prev_cum
            frac = 0 if pv == 0 else need / pv
            return t - 1 + frac
    return None

def build_cashflow_table(investment, lifetime_years, revenue_per_year, cost_per_year, tax_rate, wacc):
    years = list(range(0, int(lifetime_years) + 1))
    cf = []
    rows = []
    for y in years:
        if y == 0:
            cf.append(-abs(investment))
            rows.append({"Năm": 0, "Doanh thu": 0.0, "Chi phí": 0.0, "Lợi nhuận trước thuế": 0.0, "Thuế": 0.0, "Dòng tiền": cf[-1]})
        else:
            ebt = revenue_per_year - cost_per_year
            tax = max(0.0, ebt) * tax_rate
            ocf = ebt - tax
            cf.append(ocf)
            rows.append({"Năm": y, "Doanh thu": revenue_per_year, "Chi phí": cost_per_year, "Lợi nhuận trước thuế": ebt, "Thuế": tax, "Dòng tiền": ocf})
    npv = sum(cf[t] / ((1 + wacc) ** t) for t in range(len(cf)))
    irr = irr_bisection(cf)
    pp = payback_period(cf)
    dpp = discounted_payback_period(cf, wacc)
    return pd.DataFrame(rows), cf, npv, irr, pp, dpp

def format_period(x):
    return "Không hoàn vốn" if x is None else f"{x:.2f} năm"

def show_metrics(npv, irr, pp, dpp, currency="đ"):
    c1, c2, c3, c4 = st.columns(4)
    with c1: st.metric("NPV", f"{npv:,.0f} {currency}")
    with c2: st.metric("IRR", "-" if irr is None else f"{irr*100:.2f}%")
    with c3: st.metric("PP (hoàn vốn)", format_period(pp))
    with c4: st.metric("DPP (chiết khấu)", format_period(dpp))

# ============== 1) UPLOAD ==============
uploaded = st.file_uploader("1) Tải file Word (.docx) chứa phương án kinh doanh", type=["docx"])
doc_text = None
if uploaded:
    file_bytes = uploaded.getvalue()
    try:
        doc_text = read_docx_text(file_bytes)
        with st.expander("Xem nhanh nội dung trích từ Word"):
            st.text(doc_text[:5000] + ("\n...\n(trunc)" if len(doc_text) > 5000 else ""))
    except Exception as e:
        st.error(f"Lỗi đọc file .docx: {e}")

# Nếu người dùng thay file mới → reset form & analysis (để tránh rò dữ liệu)
if uploaded:
    st.session_state.analysis_ctx = st.session_state.analysis_ctx  # no-op (giữ)
else:
    # Không có file → tránh dùng dữ liệu cũ
    st.session_state.pending_extract = None
    st.session_state.analysis_ctx = None

# ============== 2) LỌC DỮ LIỆU VỚI AI ==============
st.subheader("2) Lọc dữ liệu dự án bằng AI")
st.caption("Trích xuất: **Vốn đầu tư, Vòng đời (năm), Doanh thu/năm, Chi phí/năm, WACC, Thuế**.")

if uploaded and st.button("🔎 Lọc dữ liệu với AI", key="btn_extract_ai"):
    if not doc_text:
        st.error("Không đọc được nội dung file.")
    else:
        api_key = st.secrets.get("GEMINI_API_KEY", None)
        if not api_key:
            st.error("Thiếu GEMINI_API_KEY trong Secrets của Streamlit.")
        else:
            try:
                client = genai.Client(api_key=api_key)
                model_name = "gemini-2.5-flash"
                prompt = f"""
Bạn là chuyên gia phân tích dự án. TRẢ VỀ DUY NHẤT JSON theo schema:
{{
  "investment": "<số>",
  "lifetime_years": "<số nguyên>",
  "revenue_per_year": "<số>",
  "cost_per_year": "<số>",
  "wacc": "<0.13 hoặc 13%>",
  "tax_rate": "<0.2 hoặc 20%>",
  "notes": "<tuỳ chọn>"
}}
Không thêm giải thích ngoài JSON.

Nội dung Word:
\"\"\"{doc_text[:12000]}\"\"\"  # cắt bớt nếu quá dài
                """.strip()

                with st.spinner("AI đang trích xuất thông tin..."):
                    resp = client.models.generate_content(model=model_name, contents=prompt)
                    raw = resp.text or ""
                    parsed = safe_json_loads(raw)
                    if not parsed:
                        st.error("AI trả về JSON không hợp lệ. Hãy thử lại hoặc nhập tay.")
                    else:
                        # Ghép vào pending_extract để merge vào form_vals ở lần rerun trước khi render form
                        extracted = {
                            "investment": parse_numbers(parsed, "investment"),
                            "lifetime_years": int(parse_numbers(parsed, "lifetime_years", 0) or 0),
                            "revenue_per_year": parse_numbers(parsed, "revenue_per_year"),
                            "cost_per_year": parse_numbers(parsed, "cost_per_year"),
                            "wacc": parse_numbers(parsed, "wacc"),
                            "tax_rate": parse_numbers(parsed, "tax_rate"),
                        }
                        st.session_state.pending_extract = extracted
                        st.success("Đã trích xuất. Kéo xuống để kiểm tra & hiệu chỉnh hoặc tính chỉ số.")
                        if parsed.get("notes"):
                            st.info(f"AI ghi chú: {parsed.get('notes')}")
            except APIError as e:
                st.error(f"Lỗi gọi Gemini API: {e}")
            except Exception as e:
                st.error(f"Lỗi không xác định khi gọi AI: {e}")

# ============== MERGE EXTRACT → FORM_VALS (trước khi render form) ==============
if st.session_state.pending_extract:
    # Chỉ cập nhật các trường có giá trị (không None)
    for k, v in st.session_state.pending_extract.items():
        if v is not None:
            st.session_state.form_vals[k] = v
    st.session_state.pending_extract = None  # dùng xong thì clear

# ============== 3) FORM NHẬP / HIỆU CHỈNH ==============
st.subheader("3) Kiểm tra & hiệu chỉnh thông số")
with st.form("inputs"):
    c1, c2, c3 = st.columns(3)
    # Bind trực tiếp vào session_state bằng key → giữ giá trị qua rerun
    with c1:
        st.number_input("Vốn đầu tư ban đầu", min_value=0.0, step=1_000_000.0, format="%.0f",
                        key="form_investment", value=st.session_state.form_vals["investment"])
        st.number_input("Vòng đời dự án (năm)", min_value=1, step=1,
                        key="form_lifetime_years", value=int(st.session_state.form_vals["lifetime_years"]))
    with c2:
        st.number_input("Doanh thu mỗi năm", min_value=0.0, step=1_000_000.0, format="%.0f",
                        key="form_revenue_per_year", value=st.session_state.form_vals["revenue_per_year"])
        st.number_input("Chi phí mỗi năm", min_value=0.0, step=1_000_000.0, format="%.0f",
                        key="form_cost_per_year", value=st.session_state.form_vals["cost_per_year"])
    with c3:
        st.number_input("WACC (vd 0.13 = 13%)", min_value=0.0, max_value=5.0, step=0.005, format="%.3f",
                        key="form_wacc", value=st.session_state.form_vals["wacc"])
        st.number_input("Thuế suất (vd 0.20 = 20%)", min_value=0.0, max_value=1.0, step=0.01, format="%.2f",
                        key="form_tax_rate", value=st.session_state.form_vals["tax_rate"])

    agree_assumption = st.checkbox(
        "Giả định đơn giản hóa: OCF = (Doanh thu - Chi phí) × (1 - Thuế). Không tính KH/giá trị thu hồi.",
        value=True
    )
    submitted = st.form_submit_button("🚀 Tạo bảng dòng tiền & Tính chỉ số")

# Cập nhật form_vals từ session_state keys (để giữ chỉnh sửa người dùng)
st.session_state.form_vals.update({
    "investment": float(st.session_state.get("form_investment", st.session_state.form_vals["investment"])),
    "lifetime_years": int(st.session_state.get("form_lifetime_years", st.session_state.form_vals["lifetime_years"])),
    "revenue_per_year": float(st.session_state.get("form_revenue_per_year", st.session_state.form_vals["revenue_per_year"])),
    "cost_per_year": float(st.session_state.get("form_cost_per_year", st.session_state.form_vals["cost_per_year"])),
    "wacc": float(st.session_state.get("form_wacc", st.session_state.form_vals["wacc"])),
    "tax_rate": float(st.session_state.get("form_tax_rate", st.session_state.form_vals["tax_rate"])),
})

# ============== 4&5) TÍNH TOÁN & HIỂN THỊ ==============
if submitted:
    if not agree_assumption:
        st.warning("Vui lòng đồng ý giả định đơn giản hóa (hoặc mở rộng code để tính khấu hao/thu hồi).")
    else:
        try:
            fv = st.session_state.form_vals
            df_cf, cashflows, npv, irr, pp, dpp = build_cashflow_table(
                investment=fv["investment"],
                lifetime_years=fv["lifetime_years"],
                revenue_per_year=fv["revenue_per_year"],
                cost_per_year=fv["cost_per_year"],
                tax_rate=fv["tax_rate"],
                wacc=fv["wacc"],
            )

            st.subheader("4) Bảng dòng tiền dự án")
            st.dataframe(
                df_cf.style.format({
                    "Doanh thu": "{:,.0f}",
                    "Chi phí": "{:,.0f}",
                    "Lợi nhuận trước thuế": "{:,.0f}",
                    "Thuế": "{:,.0f}",
                    "Dòng tiền": "{:,.0f}",
                }),
                use_container_width=True
            )

            st.subheader("5) Các chỉ số hiệu quả")
            show_metrics(npv, irr, pp, dpp, currency="đ")

            with st.expander("Chi tiết tham số & CF"):
                st.write({**fv, "cashflows": cashflows})

            # Lưu để AI phân tích
            st.session_state.analysis_ctx = {
                **fv,
                "npv": float(npv),
                "irr": None if irr is None else float(irr),
                "pp": pp,
                "dpp": dpp,
            }
            st.success("Đã lưu kết quả. Kéo xuống để yêu cầu AI phân tích bất cứ lúc nào.")
        except Exception as e:
            st.error(f"Lỗi khi tạo bảng dòng tiền / tính chỉ số: {e}")

# ============== 6) AI PHÂN TÍCH (độc lập) ==============
st.subheader("6) Phân tích hiệu quả dự án bằng AI")
ctx = st.session_state.analysis_ctx
if not ctx:
    st.info("Chưa có dữ liệu để phân tích. Hãy tính chỉ số trước.")
else:
    irr_text = "N/A" if ctx["irr"] is None else f"{ctx['irr']*100:.2f}%"
    pp_text = "Không hoàn vốn" if ctx["pp"] is None else f"{ctx['pp']:.2f} năm"
    dpp_text = "Không hoàn vốn" if ctx["dpp"] is None else f"{ctx['dpp']:.2f} năm"

    analysis_prompt = f"""
Bạn là chuyên gia thẩm định dự án. Phân tích ngắn gọn (≤4 đoạn)
về NPV, IRR, PP, DPP, so sánh với WACC và các rủi ro chính.

Thông số:
- Vốn đầu tư: {ctx['investment']:,.0f} đ
- Vòng đời: {ctx['lifetime_years']} năm
- Doanh thu/năm: {ctx['revenue_per_year']:,.0f} đ
- Chi phí/năm: {ctx['cost_per_year']:,.0f} đ
- Thuế suất: {ctx['tax_rate']:.2f}
- WACC: {ctx['wacc']:.3f}

Kết quả:
- NPV: {ctx['npv']:,.0f} đ
- IRR: {irr_text}
- PP: {pp_text}
- DPP: {dpp_text}
""".strip()

    if st.button("🧠 Yêu cầu AI phân tích", key="btn_ai_analyze"):
        api_key = st.secrets.get("GEMINI_API_KEY")
        if not api_key:
            st.error("Thiếu GEMINI_API_KEY trong Secrets.")
        else:
            try:
                client = genai.Client(api_key=api_key)
                with st.spinner("AI đang phân tích..."):
                    resp = client.models.generate_content(model="gemini-2.5-flash", contents=analysis_prompt)
                    ai_text = getattr(resp, "text", None)
                    if not ai_text:
                        try:
                            ai_text = resp.candidates[0].content.parts[0].text
                        except Exception:
                            ai_text = None
                    if ai_text:
                        st.markdown("**Kết quả phân tích từ AI:**")
                        st.info(ai_text)
                    else:
                        st.warning("Không lấy được nội dung phản hồi từ AI. Hãy thử lại.")
            except APIError as e:
                st.error(f"Lỗi gọi Gemini API: {e}")
            except Exception as e:
                st.error(f"Đã xảy ra lỗi khi gọi AI: {e}")

# ============== GỢI Ý MỞ RỘNG ==============
with st.expander("⚙️ Gợi ý mở rộng (tùy chọn)"):
    st.markdown("""
- Khấu hao/giá trị thu hồi; kịch bản (O/C/P); phân tích độ nhạy; xuất Excel/CSV.
""")
