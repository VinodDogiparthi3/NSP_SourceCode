from app.models import SourceRecord


DEMO_RECORDS = [
    SourceRecord(
        record_id="demo-affiliation-001",
        contactid="demo-contact-001",
        individual_type="Athlete",
        federation="Saudi Volleyball Federation",
        city="الدمام",
        region="المنطقة الشرقية",
        distance=0.326,
        document_text=(
            "Individual Type: Athlete. Gender: Female. Age: 19. Status: Active. "
            "Nationality: Saudi Arabia. Federation: Saudi Volleyball Federation. "
            "Sport: كرة الطائرة. Athlete Type: Amateur. Age Group: Youth. "
            "City: الدمام. Region: المنطقة الشرقية."
        ),
    ),
    SourceRecord(
        record_id="demo-affiliation-002",
        contactid="demo-contact-002",
        individual_type="Coach",
        federation="Saudi Volleyball Federation",
        city="الخبر",
        region="المنطقة الشرقية",
        distance=0.391,
        document_text=(
            "Individual Type: Coach. Gender: Male. Age: 34. Status: Active. "
            "Nationality: Saudi Arabia. Federation: Saudi Volleyball Federation. "
            "Sport: كرة الطائرة. Athlete Type: Not applicable. "
            "City: الخبر. Region: المنطقة الشرقية."
        ),
    ),
    SourceRecord(
        record_id="demo-affiliation-003",
        contactid="demo-contact-003",
        individual_type="Referee",
        federation="Saudi Volleyball Federation",
        city="الرياض",
        region="منطقة الرياض",
        distance=0.448,
        document_text=(
            "Individual Type: Referee. Gender: Female. Age: 28. Status: Active. "
            "Nationality: Saudi Arabia. Federation: Saudi Volleyball Federation. "
            "Sport: كرة الطائرة. Athlete Type: Not applicable. "
            "City: الرياض. Region: منطقة الرياض."
        ),
    ),
]


def demo_retrieve(question: str, top_k: int) -> list[SourceRecord]:
    # Representative records keep the complete UX testable before GCP is connected.
    del question
    return DEMO_RECORDS[: min(top_k, len(DEMO_RECORDS))]


def demo_answer(question: str, records: list[SourceRecord]) -> str:
    if any(char in question for char in "ابتثجحخدذرزسشصضطظعغفقكلمنهوي"):
        return (
            f"وجدت {len(records)} سجلات تجريبية ذات صلة. تعرض السجلات أفراداً مرتبطين "
            "بالاتحاد السعودي للكرة الطائرة في عدة أدوار، وتشمل رياضية ومدرباً وحكماً. "
            "افتح قسم المصادر لمراجعة التفاصيل. هذه معاينة تجريبية؛ فعّل اتصال GCP "
            "للحصول على نتائج NSP الحقيقية."
        )
    return (
        f"I found {len(records)} relevant demo affiliation records. The sample includes "
        "an athlete, a coach, and a referee connected with the Saudi Volleyball "
        "Federation across several cities. Open the source records below to inspect "
        "the exact grounded details. This is demo data; switch on the GCP connection "
        "for live NSP results."
    )

