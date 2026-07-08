"""Генерация PDF-отчёта по заявкам за период (день/неделя/месяц).

Отчёт учитывает только автосалоны, включённые в рассылку (/salons), и включает:
количество заявок, топ салонов по заявкам, статистику «Кредит выдан»,
среднюю процентную ставку по одобренным заявкам (общую и по салонам),
статистику отказов (общую и по салонам) — с процентными долями/конверсией
везде, где это применимо, и графики к каждому разделу.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    HRFlowable,
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from bot.storage import ApplicationStorage
from bot.zenit_conditions import format_money
from bot.zenit_statuses import status_category
from config import now_local

logger = logging.getLogger(__name__)

PERIOD_LABELS = {"day": "день", "week": "неделю", "month": "месяц"}
REPORT_AUTHOR = "Лебедев Даниил Михайлович"
REPORT_ROLE = "Менеджер прямых продаж автокредитов"
REPORT_ORG = "ПАО Банк ЗЕНИТ"
ISSUED_STATUS_TEXT = "кредит выдан"
MAX_CHART_DEALERS = 12
MAX_JOURNAL_ROWS = 25

# Единая ширина содержимого страницы (A4, поля 36pt слева/справа) — все таблицы
# и все графики подгоняются под неё.
CONTENT_WIDTH = 500
COLS_2 = [300, 200]                 # два столбца (сводка)
COLS_3_SIMPLE = [300, 90, 110]      # салон / число / короткий %
COLS_3_WIDE = [230, 90, 180]        # салон / число / длинная подпись-доля
COLS_JOURNAL = [55, 55, 150, 100, 140]


ZENIT_TEAL = colors.HexColor("#3E6E74")          # основной
ZENIT_TEAL_DARK = colors.HexColor("#2A4D52")      # заголовки, акценты потемнее
ZENIT_TEAL_TINT = colors.HexColor("#E9F1F1")      # подложка чётных строк таблиц
ZENIT_GOLD = colors.HexColor("#B8863B")           # ставка/проценты
ZENIT_GREEN = colors.HexColor("#3C7A5E")          # «кредит выдан»
ZENIT_RED = colors.HexColor("#A23B3B")            # отказы
ZENIT_GREY_TEXT = colors.HexColor("#5B6366")      # второстепенный текст

_FONT_DIRS = [
    Path(matplotlib.get_data_path()) / "fonts" / "ttf",
    Path("/usr/share/fonts/truetype/dejavu"),
]

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
LOGO_PATH = _ASSETS_DIR / "zenit_logo.png"
SEAL_PATH = _ASSETS_DIR / "zenit_seal.png"
SIGNATURE_PATH = _ASSETS_DIR / "zenit_signature.png"

# Логотип в шапке отчёта — крупный, хорошо читаемый элемент фирменного стиля.
LOGO_MAX_WIDTH = 190
LOGO_MAX_HEIGHT = 108


def _fit_size(path: Path, max_width: float, max_height: float) -> tuple[float, float] | None:
    """Вписывает изображение в рамку max_width x max_height с сохранением пропорций."""
    if not path.exists():
        return None
    try:
        with PILImage.open(path) as im:
            width_px, height_px = im.size
    except Exception:
        logger.warning("Не удалось прочитать изображение %s", path)
        return None
    scale = min(max_width / width_px, max_height / height_px)
    return width_px * scale, height_px * scale


def _logo_image(max_width: float = LOGO_MAX_WIDTH, max_height: float = LOGO_MAX_HEIGHT) -> RLImage | None:
    """Логотип банка для шапки отчёта, вписанный в рамку с сохранением пропорций."""
    size = _fit_size(LOGO_PATH, max_width, max_height)
    if size is None:
        return None
    return RLImage(str(LOGO_PATH), width=size[0], height=size[1])


class _SignatureBlock(Flowable):
    """Подпись и круглая печать банка """

    def __init__(self, width: float = 175, height: float = 108, sig_center_fraction: float = 0.44):
        super().__init__()
        self.width = width
        self.height = height
        # Доля высоты блока (снизу), на которой должен стоять центр подписи.
        # 0.44 примерно соответствует второй строке ФИО ("...Михайлович") при
        # вертикальном центрировании всей строки футера — см. generate_report.
        self.sig_center_fraction = sig_center_fraction

    def draw(self) -> None:
        canvas = self.canv

        seal_size = _fit_size(SEAL_PATH, self.width * 0.98, self.height * 1.0)
        if seal_size:
            seal_w, seal_h = seal_size
            canvas.drawImage(
                str(SEAL_PATH),
                self.width - seal_w,
                0,
                width=seal_w,
                height=seal_h,
                mask="auto",
            )

        sig_size = _fit_size(SIGNATURE_PATH, self.width * 0.7, self.height * 0.42)
        if sig_size:
            sig_w, sig_h = sig_size
            sig_center_y = self.height * self.sig_center_fraction
            sig_y = max(0.0, sig_center_y - sig_h / 2)
            canvas.drawImage(
                str(SIGNATURE_PATH),
                0,
                sig_y,
                width=sig_w,
                height=sig_h,
                mask="auto",
            )



_EVENT_LABELS = {
    "new_application": "Новая заявка",
    "status_change": "Смена статуса",
    "approval_conditions": "Отправлены условия «Партнёрский+»",
}


# --- шрифты ---

def _find_font(filename: str) -> str | None:
    for directory in _FONT_DIRS:
        candidate = directory / filename
        if candidate.exists():
            return str(candidate)
    return None


def _register_fonts() -> tuple[str, str]:

    regular_name, bold_name = "DejaVuSans", "DejaVuSans-Bold"
    registered = pdfmetrics.getRegisteredFontNames()

    if regular_name not in registered:
        path = _find_font("DejaVuSans.ttf")
        if path:
            pdfmetrics.registerFont(TTFont(regular_name, path))
        else:
            logger.warning("Шрифт DejaVuSans.ttf не найден, кириллица в PDF может не отобразиться")
            regular_name = "Helvetica"

    if bold_name not in registered:
        path = _find_font("DejaVuSans-Bold.ttf")
        if path:
            pdfmetrics.registerFont(TTFont(bold_name, path))
        else:
            bold_name = regular_name

    return regular_name, bold_name




def _period_bounds(period: str, now: datetime) -> tuple[datetime, datetime]:
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
    else:
        raise ValueError(f"Неизвестный период: {period}")
    return start, end


def _bucket_key(dt: datetime, period: str) -> str:
    if period == "day":
        return dt.strftime("%H:00")
    return dt.strftime("%d.%m")


def _bucket_labels(start: datetime, end: datetime, period: str) -> list[str]:
    labels = []
    step = timedelta(hours=1) if period == "day" else timedelta(days=1)
    cur = start
    while cur < end:
        labels.append(_bucket_key(cur, period))
        cur += step
    return labels


# метрики

def _pct(part: float, total: float) -> str:
    if not total:
        return "—"
    return f"{part / total * 100:.1f}%"


def _group_counts(events: list[dict], key: str = "dealership") -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        counts[e[key]] = counts.get(e[key], 0) + 1
    return counts


def _is_issued(status: str | None) -> bool:
    return bool(status) and ISSUED_STATUS_TEXT in status.strip().lower()


# графики

def _bar_chart(labels: list[str], values: list[float], title: str, color: str,
               horizontal: bool = True, value_fmt: str = "{:.0f}") -> tuple[Path, float, float]:

    if horizontal:
        fig_w, fig_h = 7.0, max(2.3, 0.4 * len(labels))
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        ax.barh(labels, values, color=color)
        ax.set_title(title, fontsize=11)
        ax.tick_params(axis="y", labelsize=7)
        for i, v in enumerate(values):
            ax.text(v, i, f" {value_fmt.format(v)}", va="center", fontsize=6.5)
        ax.spines[["top", "right"]].set_visible(False)
    else:
        fig_w, fig_h = 7.0, 2.8
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        ax.bar(labels, values, color=color)
        ax.set_title(title, fontsize=11)
        ax.tick_params(axis="x", rotation=60, labelsize=7)
        ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    path = Path(f"/tmp/_chart_{abs(hash((title, tuple(labels))))}.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path, fig_w, fig_h


def _line_chart(labels: list[str], values: list[float], title: str, color: str) -> tuple[Path, float, float]:

    fig_w, fig_h = 7.0, 2.8
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    x = list(range(len(labels)))
    ax.plot(x, values, color=color, linewidth=2, marker="o", markersize=3.2, zorder=3)
    ax.fill_between(x, values, color=color, alpha=0.16, zorder=2)
    ax.set_title(title, fontsize=11)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=60, fontsize=7)
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", color="#DCE4E4", linewidth=0.7, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    path = Path(f"/tmp/_chart_{abs(hash((title, tuple(labels))))}.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path, fig_w, fig_h


def _donut_chart(labels: list[str], values: list[float], colors_list: list[str], title: str) -> tuple[Path, float, float]:
    fig_w, fig_h = 6.6, 4.0
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    total = sum(values) or 1
    non_zero = [(l, v, c) for l, v, c in zip(labels, values, colors_list) if v > 0]
    if not non_zero:
        non_zero = [("Нет данных", 1, "#B9C6C7")]
    plot_labels, plot_values, plot_colors = zip(*non_zero)
    wedges, _ = ax.pie(
        plot_values, colors=plot_colors, startangle=90, counterclock=False,
        wedgeprops=dict(width=0.42, edgecolor="white", linewidth=1.5),
    )
    ax.set_title(title, fontsize=11, pad=12)
    legend_labels = [f"{l} — {v} ({v / total * 100:.1f}%)" for l, v in zip(plot_labels, plot_values)]
    ax.legend(
        wedges, legend_labels, loc="upper center", bbox_to_anchor=(0.5, -0.02),
        fontsize=8.5, frameon=False, ncol=1,
    )
    ax.set(aspect="equal")
    fig.subplots_adjust(top=0.88, bottom=0.30)
    path = Path(f"/tmp/_chart_{abs(hash((title, tuple(labels))))}.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path, fig_w, fig_h


def _grouped_bar_chart(
    labels: list[str], series: list[tuple[str, list[float], str]], title: str,
) -> tuple[Path, float, float]:
    fig_w, fig_h = 7.0, max(2.9, 0.5 * len(labels) + 0.4)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    n = len(series)
    group_span = 0.72
    bar_h = group_span / n
    positions = list(range(len(labels)))
    for i, (name, values, color) in enumerate(series):
        offsets = [p - group_span / 2 + bar_h * (i + 0.5) for p in positions]
        ax.barh(offsets, values, height=bar_h * 0.92, color=color, label=name)
    ax.set_yticks(positions)
    ax.set_yticklabels(labels, fontsize=7)
    ax.invert_yaxis()
    ax.set_title(title, fontsize=11)
    ax.legend(
        fontsize=8, frameon=False, loc="upper center",
        bbox_to_anchor=(0.5, -0.12 if len(labels) <= 6 else -0.06), ncol=len(series),
    )
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    path = Path(f"/tmp/_chart_{abs(hash((title, tuple(labels))))}.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path, fig_w, fig_h


# таблицы

def _styled_table(rows: list[list], col_widths: list[float], regular_font: str, bold_font: str) -> Table:
    from xml.sax.saxutils import escape

    header_style = ParagraphStyle(
        "table_header", fontName=bold_font, fontSize=8, leading=10, textColor=colors.white,
    )
    body_style = ParagraphStyle(
        "table_body", fontName=regular_font, fontSize=8, leading=10,
    )

    def wrap(cell, style):
        if isinstance(cell, str):
            return Paragraph(escape(cell), style)
        return cell

    formatted_rows = [[wrap(c, header_style) for c in rows[0]]]
    for row in rows[1:]:
        formatted_rows.append([wrap(c, body_style) for c in row])

    table = Table(formatted_rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ZENIT_TEAL),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B9C6C7")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZENIT_TEAL_TINT]),
    ]))
    return table


#  основная функция

def generate_report(storage: ApplicationStorage, period: str, output_path: Path) -> Path:
    if period not in PERIOD_LABELS:
        raise ValueError(f"Неизвестный период: {period}")

    regular_font, bold_font = _register_fonts()


    now = now_local()
    start, end = _period_bounds(period, now)
    all_events = storage.get_events_between(
        start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")
    )

    enabled_dealers = storage.list_enabled_dealership_names()
    events = [e for e in all_events if e["dealership"] in enabled_dealers]

    styles = getSampleStyleSheet()
    for name in ("Normal", "Title", "Heading1", "Heading2"):
        styles[name].fontName = regular_font
    styles["Title"].fontName = bold_font
    styles["Title"].textColor = ZENIT_TEAL_DARK
    styles["Title"].fontSize = 19
    styles["Title"].leading = 23
    styles["Heading2"].fontName = bold_font
    styles["Heading2"].textColor = ZENIT_TEAL_DARK
    small_style = ParagraphStyle("small", parent=styles["Normal"], fontSize=7.5, leading=9)
    generated_style = ParagraphStyle(
        "generated", parent=styles["Normal"], fontSize=8, textColor=ZENIT_GREY_TEXT,
    )

    period_label = PERIOD_LABELS[period]
    header_text = [
        Paragraph("Отчёт по заявкам на автокредит", styles["Title"]),
        Spacer(1, 5),
        Paragraph(
            f"За {period_label}: {start.strftime('%d.%m.%Y')} – {(end - timedelta(seconds=1)).strftime('%d.%m.%Y %H:%M')}",
            styles["Normal"],
        ),
        Spacer(1, 8),
        Paragraph(f"Сформирован: {now.strftime('%d.%m.%Y %H:%M:%S')}", generated_style),
    ]

    story = []
    logo = _logo_image()
    if logo is not None:
        header_table = Table([[header_text, logo]], colWidths=[305, CONTENT_WIDTH - 305])
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(header_table)
    else:
        story.extend(header_text)
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=1.6, color=ZENIT_TEAL, spaceBefore=0, spaceAfter=0))
    story.append(Spacer(1, 14))

    tmp_files: list[Path] = []
    img_w = CONTENT_WIDTH

    if not events:
        story.append(Paragraph(
            "За выбранный период не зафиксировано событий по включённым автосалонам.",
            styles["Normal"],
        ))
    else:
        new_events = [e for e in events if e["event_type"] == "new_application"]
        total_applications = len(new_events)
        dealer_app_counts = _group_counts(new_events)

        issued_events = [e for e in events if _is_issued(e["status"])]
        total_issued = len(issued_events)
        dealer_issued_counts = _group_counts(issued_events)

        decline_events = [e for e in events if status_category(None, e["status"]) == "decline"]
        total_declines = len(decline_events)
        dealer_decline_counts = _group_counts(decline_events)

        rate_events = [e for e in events if e["event_type"] == "approval_conditions" and e["percentage"] is not None]
        overall_rate = (
            sum(e["percentage"] for e in rate_events) / len(rate_events) if rate_events else None
        )
        dealer_rates: dict[str, float] = {}
        for dealer in {e["dealership"] for e in rate_events}:
            vals = [e["percentage"] for e in rate_events if e["dealership"] == dealer]
            dealer_rates[dealer] = sum(vals) / len(vals)

        amount_events = [e for e in events if e["event_type"] == "approval_conditions" and e["loan_amount"] is not None]
        overall_amount = (
            sum(e["loan_amount"] for e in amount_events) / len(amount_events) if amount_events else None
        )
        dealer_amounts: dict[str, float] = {}
        for dealer in {e["dealership"] for e in amount_events}:
            vals = [e["loan_amount"] for e in amount_events if e["dealership"] == dealer]
            dealer_amounts[dealer] = sum(vals) / len(vals)

        top_dealer_apps = max(dealer_app_counts.items(), key=lambda kv: kv[1]) if dealer_app_counts else None
        top_dealer_issued = max(dealer_issued_counts.items(), key=lambda kv: kv[1]) if dealer_issued_counts else None
        top_dealer_decline = max(dealer_decline_counts.items(), key=lambda kv: kv[1]) if dealer_decline_counts else None

        summary_rows = [["Показатель", "Значение"]]
        summary_rows.append(["Всего заявок", str(total_applications)])
        if top_dealer_apps:
            summary_rows.append([
                "Топ салон по заявкам",
                f"{top_dealer_apps[0]} — {top_dealer_apps[1]} ({_pct(top_dealer_apps[1], total_applications)})",
            ])
        summary_rows.append([
            "«Кредит выдан»",
            f"{total_issued} ({_pct(total_issued, total_applications)} от заявок)",
        ])
        if top_dealer_issued:
            summary_rows.append(["Топ салон по «Кредит выдан»", f"{top_dealer_issued[0]} — {top_dealer_issued[1]}"])
        if overall_rate is not None:
            summary_rows.append(["Средняя ставка по одобренным", f"{overall_rate:.2f}%"])
        if overall_amount is not None:
            summary_rows.append(["Средняя сумма кредита по одобренным", f"{format_money(overall_amount)} руб."])
        summary_rows.append([
            "Отказы (всего)",
            f"{total_declines} ({_pct(total_declines, total_applications)} от заявок)",
        ])
        if top_dealer_decline:
            summary_rows.append(["Топ салон по отказам", f"{top_dealer_decline[0]} — {top_dealer_decline[1]}"])

        story.append(Paragraph("Сводка", styles["Heading2"]))
        story.append(_styled_table(summary_rows, COLS_2, regular_font, bold_font))
        story.append(Spacer(1, 14))

        other_count = max(total_applications - total_issued - total_declines, 0)
        donut_path, dw, dh = _donut_chart(
            ["Кредит выдан", "Отказ", "В обработке / другое"],
            [total_issued, total_declines, other_count],
            ["#3C7A5E", "#A23B3B", "#B9C6C7"],
            "Структура заявок за период",
        )
        tmp_files.append(donut_path)
        story.append(RLImage(str(donut_path), width=img_w * 0.72, height=img_w * 0.72 * dh / dw))
        story.append(Spacer(1, 14))

        labels = _bucket_labels(start, end, period)
        counts_by_bucket = {label: 0 for label in labels}
        for e in new_events:
            dt = datetime.strptime(e["created_at"], "%Y-%m-%d %H:%M:%S")
            key = _bucket_key(dt, period)
            if key in counts_by_bucket:
                counts_by_bucket[key] += 1
        values = [counts_by_bucket[label] for label in labels]
        chart_path, fw, fh = _line_chart(labels, values, "Новые заявки по времени", "#3E6E74")
        tmp_files.append(chart_path)
        story.append(Paragraph("1. Количество заявок — динамика по времени", styles["Heading2"]))
        story.append(RLImage(str(chart_path), width=img_w, height=img_w * fh / fw))
        story.append(Spacer(1, 14))

        top_apps_sorted = sorted(dealer_app_counts.items(), key=lambda kv: kv[1], reverse=True)
        rows = [["Салон", "Заявок", "% от всех заявок"]]
        for name, cnt in top_apps_sorted:
            rows.append([name, str(cnt), _pct(cnt, total_applications)])
        story.append(Paragraph("2. Топ салонов по заявкам", styles["Heading2"]))
        story.append(_styled_table(rows, COLS_3_SIMPLE, regular_font, bold_font))
        story.append(Spacer(1, 10))

        chart_data = top_apps_sorted[:MAX_CHART_DEALERS][::-1]
        chart_path, fw, fh = _bar_chart(
            [n for n, _ in chart_data], [c for _, c in chart_data],
            "Заявки по автосалонам", "#3E6E74",
        )
        tmp_files.append(chart_path)
        story.append(RLImage(str(chart_path), width=img_w, height=img_w * fh / fw))
        story.append(Spacer(1, 14))

        story.append(Paragraph("3–4. Статус «Кредит выдан»", styles["Heading2"]))
        story.append(Paragraph(
            f"Всего: {total_issued} из {total_applications} заявок ({_pct(total_issued, total_applications)}).",
            styles["Normal"],
        ))
        if dealer_issued_counts:
            issued_sorted = sorted(dealer_issued_counts.items(), key=lambda kv: kv[1], reverse=True)
            rows = [["Салон", "Кредит выдан", "Конверсия (от заявок салона)"]]
            for name, cnt in issued_sorted:
                rows.append([name, str(cnt), _pct(cnt, dealer_app_counts.get(name, 0))])
            story.append(_styled_table(rows, COLS_3_WIDE, regular_font, bold_font))
            story.append(Spacer(1, 10))

            chart_data = issued_sorted[:MAX_CHART_DEALERS][::-1]
            chart_path, fw, fh = _bar_chart(
                [n for n, _ in chart_data], [c for _, c in chart_data],
                "«Кредит выдан» по автосалонам", "#3C7A5E",
            )
            tmp_files.append(chart_path)
            story.append(RLImage(str(chart_path), width=img_w, height=img_w * fh / fw))
            story.append(Spacer(1, 14))

            compare_labels = [n for n, _ in chart_data]
            compare_apps = [dealer_app_counts.get(n, 0) for n in compare_labels]
            compare_issued = [dealer_issued_counts.get(n, 0) for n in compare_labels]
            chart_path, fw, fh = _grouped_bar_chart(
                compare_labels,
                [("Заявки", compare_apps, "#3E6E74"), ("Кредит выдан", compare_issued, "#3C7A5E")],
                "Заявки и «кредит выдан» — сравнение по автосалонам",
            )
            tmp_files.append(chart_path)
            story.append(RLImage(str(chart_path), width=img_w, height=img_w * fh / fw))
        else:
            story.append(Paragraph("За период случаев «Кредит выдан» не зафиксировано.", styles["Normal"]))
        story.append(Spacer(1, 14))

        story.append(Paragraph("5–6. Средняя процентная ставка по одобренным заявкам", styles["Heading2"]))
        if overall_rate is not None:
            story.append(Paragraph(f"Средняя ставка по всем включённым салонам: {overall_rate:.2f}%.", styles["Normal"]))
            if dealer_rates:
                rate_sorted = sorted(dealer_rates.items(), key=lambda kv: kv[1], reverse=True)
                rows = [["Салон", "Средняя ставка", "Отклонение от общей средней"]]
                for name, rate in rate_sorted:
                    delta_pp = rate - overall_rate
                    delta_rel = (delta_pp / overall_rate * 100) if overall_rate else 0
                    sign = "+" if delta_pp >= 0 else ""
                    rows.append([
                        name,
                        f"{rate:.2f}%",
                        f"{sign}{delta_pp:.2f} п.п. ({sign}{delta_rel:.1f}%)",
                    ])
                story.append(_styled_table(rows, COLS_3_WIDE, regular_font, bold_font))
                story.append(Spacer(1, 10))

                chart_data = rate_sorted[:MAX_CHART_DEALERS][::-1]
                chart_path, fw, fh = _bar_chart(
                    [n for n, _ in chart_data], [c for _, c in chart_data],
                    "Средняя ставка по автосалонам, %", "#B8863B", value_fmt="{:.1f}",
                )
                tmp_files.append(chart_path)
                story.append(RLImage(str(chart_path), width=img_w, height=img_w * fh / fw))
        else:
            story.append(Paragraph(
                "За период не зафиксировано отправленных условий «Партнёрский+» с процентной ставкой.",
                styles["Normal"],
            ))
        story.append(Spacer(1, 14))

        story.append(Paragraph("7–8. Средняя сумма кредита по одобренным заявкам", styles["Heading2"]))
        if overall_amount is not None:
            story.append(Paragraph(
                f"Средняя сумма кредита по всем включённым салонам: {format_money(overall_amount)} руб.",
                styles["Normal"],
            ))
            if dealer_amounts:
                amount_sorted = sorted(dealer_amounts.items(), key=lambda kv: kv[1], reverse=True)
                rows = [["Салон", "Средняя сумма кредита", "Отклонение от общей средней"]]
                for name, amount in amount_sorted:
                    delta_abs = amount - overall_amount
                    delta_rel = (delta_abs / overall_amount * 100) if overall_amount else 0
                    sign = "+" if delta_abs >= 0 else ""
                    rows.append([
                        name,
                        f"{format_money(amount)} руб.",
                        f"{sign}{format_money(delta_abs)} руб. ({sign}{delta_rel:.1f}%)",
                    ])
                story.append(_styled_table(rows, COLS_3_WIDE, regular_font, bold_font))
                story.append(Spacer(1, 10))

                chart_data = amount_sorted[:MAX_CHART_DEALERS][::-1]
                chart_path, fw, fh = _bar_chart(
                    [n for n, _ in chart_data], [c for _, c in chart_data],
                    "Средняя сумма кредита по автосалонам, руб.", "#3E6E74", value_fmt="{:,.0f}",
                )
                tmp_files.append(chart_path)
                story.append(RLImage(str(chart_path), width=img_w, height=img_w * fh / fw))
        else:
            story.append(Paragraph(
                "За период не зафиксировано отправленных условий «Партнёрский+» с суммой кредита.",
                styles["Normal"],
            ))
        story.append(Spacer(1, 14))

        story.append(Paragraph("9–10. Отказы", styles["Heading2"]))
        story.append(Paragraph(
            f"Всего отказов: {total_declines} из {total_applications} заявок ({_pct(total_declines, total_applications)}).",
            styles["Normal"],
        ))
        if dealer_decline_counts:
            decline_sorted = sorted(dealer_decline_counts.items(), key=lambda kv: kv[1], reverse=True)
            rows = [["Салон", "Отказов", "Доля отказов (от заявок салона)"]]
            for name, cnt in decline_sorted:
                rows.append([name, str(cnt), _pct(cnt, dealer_app_counts.get(name, 0))])
            story.append(_styled_table(rows, COLS_3_WIDE, regular_font, bold_font))
            story.append(Spacer(1, 10))

            chart_data = decline_sorted[:MAX_CHART_DEALERS][::-1]
            chart_path, fw, fh = _bar_chart(
                [n for n, _ in chart_data], [c for _, c in chart_data],
                "Отказы по автосалонам", "#A23B3B",
            )
            tmp_files.append(chart_path)
            story.append(RLImage(str(chart_path), width=img_w, height=img_w * fh / fw))
        else:
            story.append(Paragraph("За период отказов не зафиксировано.", styles["Normal"]))
        story.append(Spacer(1, 14))

        sorted_events = sorted(events, key=lambda e: e["created_at"], reverse=True)
        shown = sorted_events[:MAX_JOURNAL_ROWS]
        detail_rows = [["Время", "Заявка", "Салон", "Событие", "Статус"]]
        for e in shown:
            dt = datetime.strptime(e["created_at"], "%Y-%m-%d %H:%M:%S")
            detail_rows.append([
                dt.strftime("%d.%m %H:%M"),
                str(e["app_id"]),
                Paragraph(e["dealership"], small_style),
                _EVENT_LABELS.get(e["event_type"], e["event_type"]),
                Paragraph(e["status"] or "—", small_style),
            ])
        story.append(Paragraph("Журнал событий (для справки)", styles["Heading2"]))
        if len(sorted_events) > MAX_JOURNAL_ROWS:
            story.append(Paragraph(
                f"Показаны последние {MAX_JOURNAL_ROWS} из {len(sorted_events)} событий.", styles["Normal"]
            ))
        story.append(_styled_table(detail_rows, COLS_JOURNAL, regular_font, bold_font))

    story.append(Spacer(1, 24))
    story.append(HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#B9C6C7"), spaceBefore=0, spaceAfter=14))

    role_style = ParagraphStyle("role", parent=styles["Normal"], fontSize=9, leading=12)
    name_style = ParagraphStyle(
        "name", parent=styles["Normal"], fontName=bold_font, fontSize=10, leading=13,
        alignment=TA_RIGHT, textColor=ZENIT_TEAL_DARK,
    )

    role_cell = [
        Paragraph(REPORT_ROLE, role_style),
        Paragraph(REPORT_ORG, role_style),
    ]
    name_cell = Paragraph(REPORT_AUTHOR, name_style)

    footer_table = Table(
        [[role_cell, _SignatureBlock(width=175, height=108), name_cell]],
        colWidths=[180, 175, 145],
    )
    footer_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(footer_table)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36,
        title="Отчёт по заявкам на автокредит",
    )
    doc.build(story)

    for tmp in tmp_files:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    return output_path
