import io
import datetime
import discord
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


async def build_elo_chart(
    history,
    username: str,
    label: str,
) -> discord.File | None:

    if not history:
        return None

    times = [datetime.datetime.fromisoformat(str(r["timestamp"])) for r in history]
    values = [r["elo_after"] for r in history]

    # добавляем стартовую точку
    times.insert(0, times[0] - datetime.timedelta(seconds=1))
    values.insert(0, history[0]["elo_before"])

    fig, ax = plt.subplots(figsize=(11, 5), facecolor="#2C2F33")
    ax.set_facecolor("#2C2F33")

    color = "#7289DA"
    ax.plot(times, values, color=color, linewidth=2.0, zorder=3)
    ax.fill_between(times, values, alpha=0.12, color=color)

    # маркеры
    ax.scatter(times[1:], values[1:], color=color, s=30, zorder=4)

    ax.set_title(
        f"История ELO  ·  {username}  ·  {label}",
        color="white", fontsize=13, pad=12,
    )
    ax.set_ylabel("ELO", color="#99AAB5")
    ax.tick_params(colors="#99AAB5", labelsize=9)
    for spine in ax.spines.values():
        spine.set_color("#555")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, alpha=0.15, color="#99AAB5", linestyle="--")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate(rotation=30)

    # последнее значение
    ax.annotate(
        f"  {values[-1]}",
        xy=(times[-1], values[-1]),
        color="white", fontsize=11, fontweight="bold",
    )

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110)
    buf.seek(0)
    plt.close(fig)
    return discord.File(buf, filename="elo.png")