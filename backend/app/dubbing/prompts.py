"""Prompt templates for the dubbing LLM stages.

All prompts are genre-neutral. Genre is passed in as an optional hint and the
glossary is extracted per-video, so nothing here is hardcoded to one show.
Use ``str.format`` with the documented fields.
"""

from __future__ import annotations

# --- Glossary extraction (data-driven, any genre) ---------------------------

GLOSSARY_SYSTEM = (
    "Bạn là biên tập viên bản địa hóa Việt. Đọc transcript tiếng Trung của một video "
    "và rút ra các thực thể cần dịch nhất quán: tên riêng, địa danh, tổ chức, chức danh/cấp bậc, "
    "thuật ngữ đặc thù của thể loại, và số liệu/đơn vị quan trọng.\n"
    "Tự suy ra thể loại từ nội dung; KHÔNG giả định trước. Chỉ ghi mục thực sự xuất hiện trong nguồn.\n"
    "Output DUY NHẤT là TSV, không markdown, không giải thích, mỗi dòng:\n"
    "TERM_ZH<TAB>TERM_VI<TAB>KIND<TAB>NOTE\n"
    "KIND thuộc: name, place, org, rank, term, number. NOTE ngắn gọn hoặc để trống."
)

GLOSSARY_USER = (
    "GENRE_HINT (tham khảo, có thể sai): {genre}\n\n"
    "TRANSCRIPT_ZH:\n{source}\n\n"
    "Trả về bảng thuật ngữ TSV (TERM_ZH<TAB>TERM_VI<TAB>KIND<TAB>NOTE)."
)

# --- Stateful chunk translation ---------------------------------------------

TRANSLATE_SYSTEM = (
    "Bạn là đạo diễn lồng tiếng Việt cho video dài. Bạn nhận glossary, tóm tắt ngữ cảnh các chunk "
    "trước (MEMORY), đuôi lời thoại trước, và các anchor ASR tiếng Trung của chunk hiện tại.\n"
    "Anchor vừa là rail thời gian, rail phủ nội dung, vừa là rail nhịp thoại. Viết lời Việt theo beat, "
    "KHÔNG dịch máy từng anchor. Mỗi dòng output là một nhịp thoại ngắn, có lực, khớp mật độ thoại gốc; "
    "với cảnh liên tục hãy tách thành nhiều dòng kế tiếp để giọng đọc chảy đều, không tạo khoảng im giả.\n"
    "Được gộp/bỏ filler/diễn đạt lại mạnh tay cho mượt, NHƯNG phải giữ fact then chốt: số liệu/đơn vị, "
    "tên riêng theo glossary, thuật ngữ/cấp bậc, phủ định, nhân quả, lời tố cáo/đe dọa/tiết lộ. "
    "Câu trả lời ngắn ngay sau một câu hỏi (ai/ở đâu/bao nhiêu/của ai) thường then chốt — không gộp mất.\n"
    "Mỗi dòng gắn với đúng một ANCHOR_ID có trong chunk. Dùng dấu ⟂ để tách hai lượt thoại trong cùng anchor.\n"
    "Output DUY NHẤT là TSV, không markdown, không giải thích, KHÔNG Hán tự, mỗi dòng:\n"
    "ANCHOR_ID<TAB>TEXT"
)

TRANSLATE_USER = (
    "GLOSSARY:\n{glossary}\n\n"
    "MEMORY (ngữ cảnh các chunk trước):\n{memory}\n\n"
    "ĐUÔI LỜI THOẠI TRƯỚC (để nối mạch, không dịch lại):\n{prev_tail}\n\n"
    "CHUNK HIỆN TẠI — ANCHORS (ANCHOR_ID<TAB>TIME<TAB>SOURCE_ZH):\n{anchors}\n\n"
    "Ngân sách nhịp đọc: tối đa khoảng {max_caption_chars} ký tự mỗi dòng hiển thị.\n"
    "Viết lời Việt cho mọi ANCHOR_ID ở trên. Sau bảng thoại, thêm tối đa 5 dòng MEMORY mới dạng:\n"
    "MEMORY<TAB>KEY<TAB>VALUE\n"
    "để chunk sau biết tên/diễn biến đã chốt. Chỉ TSV."
)

# --- Gated semantic editor --------------------------------------------------

EDITOR_SYSTEM = (
    "Bạn là biên tập viên kiểm định bản lồng tiếng Việt. Với mỗi anchor bạn thấy SOURCE_ZH, các "
    "micro-segment nguồn, và lời Việt hiện tại. Tìm lỗi thực sự: sai nghĩa, mất fact then chốt, "
    "sai tên/thuật ngữ theo glossary, còn Hán tự, hoặc lời cụt/khó đọc.\n"
    "Chỉ phát hiện có bằng chứng. Output DUY NHẤT là TSV, không markdown, mỗi dòng một trong hai dạng:\n"
    "FINDING<TAB>ANCHOR_ID<TAB>SEVERITY<TAB>EVIDENCE<TAB>ISSUE\n"
    "PATCH<TAB>ANCHOR_ID<TAB>TEXT\n"
    "SEVERITY thuộc: BLOCKER, ERROR, WARN. Chỉ đưa PATCH cho anchor vừa có FINDING mức BLOCKER hoặc ERROR. "
    "WARN chỉ ghi nhận, không patch. PATCH giữ nguyên ⟂ nếu cần tách lượt thoại, không thêm Hán tự."
)

EDITOR_USER = (
    "GLOSSARY:\n{glossary}\n\n"
    "CÁC ANCHOR CẦN RÀ SOÁT:\n{anchors}\n\n"
    "Trả về FINDING/PATCH theo schema. Nếu không có lỗi, trả về dòng trống."
)

# --- Timing repair (shorten overbudget anchors) -----------------------------

REPAIR_SYSTEM = (
    "Bạn là biên tập viên rút gọn lời lồng tiếng Việt cho khớp thời lượng. Mỗi anchor có lời hiện tại "
    "đọc QUÁ DÀI so với khung thời gian. Viết lại NGẮN HƠN, giữ trọn fact then chốt (số liệu, tên riêng "
    "theo glossary, phủ định, nhân quả, lời tố cáo/tiết lộ), bỏ filler, vẫn nghe tự nhiên.\n"
    "Output DUY NHẤT là TSV, không markdown, KHÔNG Hán tự, mỗi dòng:\n"
    "ANCHOR_ID<TAB>TEXT\n"
    "Mỗi bản viết lại phải ngắn hơn bản cũ. Giữ ⟂ nếu cần tách lượt thoại."
)

REPAIR_USER = (
    "GLOSSARY:\n{glossary}\n\n"
    "CÁC ANCHOR QUÁ DÀI (ANCHOR_ID<TAB>TARGET_CHARS<TAB>SOURCE_ZH<TAB>CURRENT_VI):\n{anchors}\n\n"
    "Viết lại ngắn hơn cho mọi ANCHOR_ID. Chỉ TSV."
)

# --- Completeness repair (expand lines that dropped source content) ----------

EXPAND_SYSTEM = (
    "Bạn là biên tập lồng tiếng Việt. Mỗi anchor có lời Việt hiện đọc QUÁ NGẮN so với câu gốc — "
    "thường vì đã BỎ SÓT ý của câu gốc.\n"
    "Với mỗi anchor: nếu lời Việt đã truyền đạt ĐỦ ý câu gốc thì GIỮ NGUYÊN; nếu còn THIẾU ý, viết lại "
    "ĐẦY ĐỦ toàn bộ ý của câu gốc, tự nhiên, dài hơn cho khớp thời lượng thoại gốc. "
    "TUYỆT ĐỐI không bịa thông tin ngoài câu gốc; giữ tên/thuật ngữ theo glossary.\n"
    "Output DUY NHẤT là TSV, KHÔNG Hán tự, mỗi dòng:\n"
    "ANCHOR_ID<TAB>TEXT"
)

EXPAND_USER = (
    "GLOSSARY:\n{glossary}\n\n"
    "CÁC ANCHOR ĐỌC QUÁ NGẮN (ANCHOR_ID<TAB>GIÂY_GỐC<TAB>SOURCE_ZH<TAB>CURRENT_VI):\n{anchors}\n\n"
    "Trả về lời Việt đầy đủ (hoặc giữ nguyên) cho mọi ANCHOR_ID. Chỉ TSV."
)
