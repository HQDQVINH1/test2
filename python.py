# app.py

import io
import json
import math
import numpy as np
import pandas as pd
import streamlit as st
from google import genai
from google.genai.errors import APIError
from docx import Document

# =========================
# CẤU HÌNH & GIAO DIỆN
# =========================
st.set_page_config(page_title="Đánh giá Phương án Kinh doanh (DOCX) 📄➡️📊", layout="wide")
st.title("Đánh giá Phương án Kinh doanh từ file Word 📄➡️📊")

st.caption(
    "Upload file Word (.docx) chứa phương án kinh doanh. Ấn **Lọc dữ liệu với AI** để trích xuất: "
    "Vốn đầu tư, Vòng đời, Doanh thu, Chi phí, WACC, Thuế. Có thể chỉnh tay sau khi AI trích xuất."
)

# =========================
# TIỆN ÍCH
# =========================
def read_docx_text(file_bytes: bytes) -> str:
    """Đọc toàn bộ text từ .docx (giữ xuống dòng cơ bản)."""
    doc = Document(io.BytesIO(file_bytes))
    paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    return "\n".join(paras)

def safe_json_loads(s: str):
    """Parse JSON an toàn, tự sửa một số lỗi phổ biến."""
    try:
        return json.loads(s)
    except Exception:
        # Thử cắt phần mở đầu/kết thúc nếu model trả thêm text
        s2 = s.strip()
        # Loại bỏ code fences nếu có
        if s2.startswith("```"):
            s2 = s2.strip("`")
            # loại bỏ gợi ý loại ngôn ngữ
            if s2.startswith("json"):
                s2 = s2[4:]
        s2 = s2.strip()
        # Cố đóng ngoặc nếu thiếu
        if s2 and s2[-1] != "}":
            s2 += "}"
        try:
            return json.loads(s2)
        except Exception:
            return None

def parse_numbers(d, key, default=None):
    """Ép kiểu số từ dict với key (chấp nhận %, , . và khoảng trắng)."""
    try:
        val = d.get(key, None)
        if val is None or (isinstance(val, str) and not val.strip()):
            return default
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).replace(",", "").replace(" ", "")
        # phần trăm
        if s.endswith("%"):
            return float(s[:-1]) / 100.0
        return float(s)
    except Exception:
        return default

def irr_bisection(cashflows, tol=1e-6, max_iter=100):
    """IRR bằng phương pháp chia đôi (tránh phụ thuộc numpy_financial)."""
    # Tìm r sao cho NPV = 0
    def npv(r):
        return sum(cf / ((1 + r) ** t) for t, cf in enumerate(cashflows))
    # Giới hạn ban đầu
    low, high = -0.9999, 10.0
    f_low, f_high = npv(low), npv(high)
    # Nếu cùng dấu, thử mở rộng
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
    """
    PP: năm hoàn vốn không chiết khấu (có phần thập phân).
    cashflows[0] thường âm (đầu tư ban đầu).
    """
    cum = 0.0
    for t, cf in enumerate(cashflows):
        cum += cf
        if cum >= 0:
            # hoàn vốn trong năm t
            # phần còn thiếu ở đầu năm t / CF của năm t
            prev_cum = cum - cf
            need = -prev_cum
            frac = 0 if cf == 0 else need / cf
            return t - 1 + frac  # vì t là cuối năm t; hoàn vốn giữa năm t -> t-1 + frac
    return None

def discounted_payback_period(cashflows, rate):
    """DPP: hoàn vốn có chiết khấu."""
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
    """
    Giả định:
    - Chi đầu tư (CapEx) tại t=0: investment (âm).
    - Dòng tiền hoạt động mỗi năm t=1..N: (Doanh thu - Chi phí) * (1 - Thuế)
    - Không tính khấu hao/giá trị thu hồi (có thể mở rộng sau).
    """
    years = list(range(0, int(lifetime_years) + 1))
    cf = []
    detail = []
    for y in years:
        if y == 0:
            cf.append(-abs(investment))
            detail.append({"Năm": 0, "Doanh thu": 0.0, "Chi phí": 0.0, "Lợi nhuận trước thuế": 0.0,
                           "Thuế": 0.0, "Dòng tiền": cf[-1]})
        else:
            ebt = revenue_per_year - cost_per_year  # EBIT ~ EBT (giả định không lãi vay & KH)
            tax = max(0.0, ebt) * tax_rate  # nếu lỗ thì thuế = 0
            ocf = (ebt - tax)
            cf.append(ocf)
            detail.append({
                "Năm": y,
                "Doanh thu": revenue_per_year,
                "Chi phí": cost_per_year,
                "Lợi nhuận trước thuế": ebt,
                "Thuế": tax,
                "Dòng tiền": ocf
            })
    # NPV
    npv = sum(cf[t] / ((1 + wacc) ** t) for t in range(len(cf)))
    # IRR
    irr = irr_bisection(cf)
    # PP & DPP
    pp = payback_period(cf)
    dpp = discounted_payback_period(cf, wacc)

    df = pd.DataFrame(detail)
    return df, cf, npv, irr, pp, dpp

def format_period(x):
    return "Không hoàn vốn" if x is None else f"{x:.2f} năm"

def show_metrics(npv, irr, pp, dpp, currency="đ"):
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("NPV", f"{npv:,.0f} {currency}")
    with c2:
        st.metric("IRR", "-" if irr is None else f"{irr*100:.2f}%")
    with c3:
        st.metric("PP (hoàn vốn)", format_period(pp))
    with c4:
        st.metric("DPP (chiết khấu)", format_period(dpp))

# =========================
# UPLOAD FILE
# =========================
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

# =========================
# AI TRÍCH XUẤT THÔNG TIN
# =========================
st.subheader("2) Lọc dữ liệu dự án bằng AI")
st.caption("Ấn nút dưới đây để AI trích xuất các trường: **Vốn đầu tư, Vòng đời (năm), Doanh thu/năm, Chi phí/năm, WACC, Thuế**.")
ai_col = st.container()

default_values = {
    "investment": None,
    "lifetime_years": None,
    "revenue_per_year": None,
    "cost_per_year": None,
    "wacc": None,
    "tax_rate": None
}

if uploaded and st.button("🔎 Lọc dữ liệu với AI"):
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
Bạn là chuyên gia phân tích dự án. Hãy TRẢ VỀ DUY NHẤT JSON theo schema sau, dựa trên nội dung Word (tiếng Việt có thể lẫn số):
{{
  "investment": "<Vốn đầu tư ban đầu, số>",
  "lifetime_years": "<Số năm vòng đời dự án, số nguyên>",
  "revenue_per_year": "<Doanh thu mỗi năm, số>",
  "cost_per_year": "<Chi phí mỗi năm, số>",
  "wacc": "<WACC, dạng số thập phân hoặc %, ví dụ 0.13 hoặc 13%>",
  "tax_rate": "<Thuế suất, dạng số thập phân hoặc %, ví dụ 0.2 hoặc 20%>",
  "notes": "<Các giả định AI suy luận thêm (nếu thiếu thông tin)>"
}}

YÊU CẦU:
- Chỉ trả JSON hợp lệ; không giải thích thêm, không text ngoài JSON.
- Nếu thiếu trường, cố gắng suy luận hoặc để chuỗi rỗng "".

Nội dung Word:
\"\"\"{doc_text[:12000]}\"\"\"  # (cắt bớt nếu quá dài)
                """.strip()

                with st.spinner("AI đang trích xuất thông tin..."):
                    resp = client.models.generate_content(model=model_name, contents=prompt)
                    raw = resp.text or ""
                    parsed = safe_json_loads(raw)
                    if not parsed:
                        st.error("AI trả về JSON không hợp lệ. Hãy thử lại hoặc nhập tay.")
                    else:
                        default_values["investment"] = parse_numbers(parsed, "investment")
                        default_values["lifetime_years"] = int(parse_numbers(parsed, "lifetime_years", 0) or 0)
                        default_values["revenue_per_year"] = parse_numbers(parsed, "revenue_per_year")
                        default_values["cost_per_year"] = parse_numbers(parsed, "cost_per_year")
                        default_values["wacc"] = parse_numbers(parsed, "wacc")
                        default_values["tax_rate"] = parse_numbers(parsed, "tax_rate")
                        st.success("Đã trích xuất xong. Bạn có thể hiệu chỉnh các giá trị bên dưới.")
                        if parsed.get("notes"):
                            st.info(f"AI ghi chú: {parsed.get('notes')}")
            except APIError as e:
                st.error(f"Lỗi gọi Gemini API: {e}")
            except Exception as e:
                st.error(f"Lỗi không xác định khi gọi AI: {e}")

# =========================
# FORM THÔNG SỐ (CHO PHÉP HIỆU CHỈNH)
# =========================
st.subheader("3) Kiểm tra & hiệu chỉnh thông số")
with st.form("inputs"):
    c1, c2, c3 = st.columns(3)
    with c1:
        investment = st.number_input("Vốn đầu tư ban đầu", min_value=0.0, value=float(default_values["investment"] or 0.0), step=1_000_000.0, format="%.0f")
        lifetime_years = st.number_input("Vòng đời dự án (năm)", min_value=1, value=int(default_values["lifetime_years"] or 5), step=1)
    with c2:
        revenue_per_year = st.number_input("Doanh thu mỗi năm", min_value=0.0, value=float(default_values["revenue_per_year"] or 0.0), step=1_000_000.0, format="%.0f")
        cost_per_year = st.number_input("Chi phí mỗi năm", min_value=0.0, value=float(default_values["cost_per_year"] or 0.0), step=1_000_000.0, format="%.0f")
    with c3:
        wacc = st.number_input("WACC (ví dụ 0.13 = 13%)", min_value=0.0, max_value=5.0, value=float(default_values["wacc"] or 0.13), step=0.005, format="%.3f")
        tax_rate = st.number_input("Thuế suất (ví dụ 0.20 = 20%)", min_value=0.0, max_value=1.0, value=float(default_values["tax_rate"] or 0.20), step=0.01, format="%.2f")

    agree_assumption = st.checkbox(
        "Giả định đơn giản hóa: Dòng tiền hoạt động = (Doanh thu - Chi phí) × (1 - Thuế). "
        "Không tính khấu hao/giá trị thu hồi.", value=True
    )

    submitted = st.form_submit_button("🚀 Tạo bảng dòng tiền & Tính chỉ số")

# =========================
# LẬP BẢNG DÒNG TIỀN + CHỈ SỐ
# =========================
if submitted:
    if not agree_assumption:
        st.warning("Vui lòng đồng ý giả định đơn giản hóa (hoặc mở rộng code để tính khấu hao/thu hồi).")
    else:
        try:
            df_cf, cashflows, npv, irr, pp, dpp = build_cashflow_table(
                investment=investment,
                lifetime_years=lifetime_years,
                revenue_per_year=revenue_per_year,
                cost_per_year=cost_per_year,
                tax_rate=tax_rate,
                wacc=wacc
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
                st.write({
                    "investment": investment,
                    "lifetime_years": lifetime_years,
                    "revenue_per_year": revenue_per_year,
                    "cost_per_year": cost_per_year,
                    "tax_rate": tax_rate,
                    "wacc": wacc,
                    "cashflows": cashflows
                })

            # =========================
            # PHÂN TÍCH AI CÁC CHỈ SỐ
            # =========================
            st.subheader("6) Phân tích hiệu quả dự án bằng AI")
            analysis_prompt = f"""
Bạn là chuyên gia thẩm định dự án. Hãy phân tích ngắn gọn, súc tích (tối đa ~4 đoạn),
trọng tâm vào NPV, IRR, PP, DPP, mức độ hấp dẫn so với WACC, và rủi ro chính.

Thông số:
- Vốn đầu tư: {investment:,.0f} đ
- Vòng đời: {lifetime_years} năm
- Doanh thu/năm: {revenue_per_year:,.0f} đ
- Chi phí/năm: {cost_per_year:,.0f} đ
- Thuế suất: {tax_rate:.2f}
- WACC: {wacc:.3f}

Kết quả:
- NPV: {npv:,.0f} đ
- IRR: {"N/A" if irr is None else f"{irr*100:.2f}%"}
- PP: {"Không hoàn vốn" if pp is None else f"{pp:.2f} năm"}
- DPP: {"Không hoàn vốn" if dpp is None else f"{dpp:.2f} năm"}

Giải thích:
- Ý nghĩa từng chỉ số với bối cảnh dự án.
- So sánh IRR với WACC (nếu IRR > WACC thì dự án có thể hấp dẫn).
- Nhận xét về độ an toàn khi NPV ~ 0.
- Nêu các rủi ro (độ nhạy doanh thu/chi phí, rủi ro lãi suất, thời gian đạt điểm hòa vốn).
"""
            if st.button("🧠 Yêu cầu AI phân tích"):
                api_key = st.secrets.get("GEMINI_API_KEY", None)
                if not api_key:
                    st.error("Thiếu GEMINI_API_KEY trong Secrets.")
                else:
                    try:
                        client = genai.Client(api_key=api_key)
                        model_name = "gemini-2.5-flash"
                        with st.spinner("AI đang phân tích..."):
                            resp = client.models.generate_content(model=model_name, contents=analysis_prompt)
                            st.markdown("**Kết quả phân tích từ AI:**")
                            st.info(resp.text)
                    except APIError as e:
                        st.error(f"Lỗi gọi Gemini API: {e}")
                    except Exception as e:
                        st.error(f"Lỗi không xác định khi gọi AI: {e}")

        except Exception as e:
            st.error(f"Lỗi khi tạo bảng dòng tiền / tính chỉ số: {e}")

# =========================
# GỢI Ý MỞ RỘNG
# =========================
with st.expander("⚙️ Gợi ý mở rộng (tùy chọn)"):
    st.markdown("""
- Thêm **khấu hao** theo phương pháp đường thẳng → ảnh hưởng thuế nhưng không ảnh hưởng dòng tiền (chỉ khi tính lợi nhuận).
- Thêm **giá trị thu hồi (salvage)** ở năm cuối.
- Cho phép **kịch bản**: lạc quan/cơ sở/bi quan cho Doanh thu & Chi phí.
- Thêm **phân tích độ nhạy** (WACC ±, Doanh thu ±, Chi phí ±).
- Xuất **Excel**/CSV bảng dòng tiền và chỉ số.
""")
