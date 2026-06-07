import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from supabase import create_client
from datetime import datetime

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
CATEGORY_ID = int(os.environ["CATEGORY_ID"]) if os.environ.get("CATEGORY_ID") else None

supabase = create_client(SUPABASE_URL, SERVICE_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def poll_orders():
    await bot.wait_until_ready()
    processed = set()
    try:
        existing = supabase.table("orders").select("id").neq("status", "pending").execute().data
        for o in existing:
            processed.add(o["id"])
    except Exception as e:
        print(f"[Polling] Błąd inicjalizacji: {e}")

    while not bot.is_closed():
        try:
            pending = supabase.table("orders").select("*").eq("status", "pending").execute().data
            for order in pending:
                if order["id"] not in processed:
                    processed.add(order["id"])
                    asyncio.create_task(handle_new_order(order))
        except Exception as e:
            print(f"[Polling] Błąd: {e}")
        await asyncio.sleep(5)


async def handle_new_order(order):
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            print(f"[Bot] Nie znaleziono serwera {GUILD_ID}")
            return

        account_data = supabase.table("steam_accounts").select("*").eq("id", order["product_id"]).execute().data
        if not account_data:
            print(f"[Bot] Brak konta dla zamówienia {order['id']}")
            return
        account = account_data[0]

        member = None
        try:
            member = await guild.fetch_member(int(order["discord_id"]))
        except Exception:
            pass

        category = guild.get_channel(CATEGORY_ID) if CATEGORY_ID else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }
        if member:
            overwrites[member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel = await guild.create_text_channel(
            name=f"zamowienie-{order['id'][:8]}",
            overwrites=overwrites,
            category=category,
            topic=f"Zamówienie ID: {order['id']}",
        )

        try:
            supabase.table("orders").update({
                "channel_id": str(channel.id),
                "status": "awaiting_payment",
            }).eq("id", order["id"]).execute()
        except Exception:
            try:
                supabase.table("orders").update({"status": "awaiting_payment"}).eq("id", order["id"]).execute()
            except Exception as e2:
                print(f"[Bot] Błąd aktualizacji statusu: {e2}")

        mention = member.mention if member else f"<@{order['discord_id']}>"

        embed = discord.Embed(
            title="🛒 Nowe zamówienie — Arcyn",
            description=f"Cześć {mention}! Twoje zamówienie zostało przyjęte.\n\nAdministrator skontaktuje się z Tobą tutaj w sprawie płatności.",
            color=0x3b82f6,
        )
        embed.add_field(name="📦 Produkt", value=account.get("name", "Konto Steam"), inline=True)
        embed.add_field(name="💰 Do zapłaty", value=f"**{account.get('price', '?')} PLN**", inline=True)
        embed.add_field(name="🆔 ID zamówienia", value=f"```{order['id']}```", inline=False)
        embed.add_field(
            name="📋 Co dalej?",
            value="1. Czekaj na instrukcje admina ws. płatności\n2. Po opłaceniu — admin potwierdza `/zaplacono`\n3. Wejdź na stronę → **Moje zamówienia** → odbierz dane",
            inline=False
        )
        embed.set_footer(text="Arcyn · Bezpieczny zakup kont Steam")
        if account.get("image_url"):
            embed.set_thumbnail(url=account["image_url"])

        await channel.send(mention, embed=embed)
        print(f"[Bot] ✅ Ticket: #{channel.name}")

    except Exception as e:
        print(f"[Bot] Błąd przy zamówieniu {order['id']}: {e}")


# ──────────────────────────────────────────────
# SLASH COMMANDS
# ──────────────────────────────────────────────

@bot.tree.command(name="zaplacono", description="Potwierdź płatność — odblokuje konto na stronie i zamknie ticket")
@app_commands.describe(order_id="ID zamówienia (z embeda na tickecie)")
@app_commands.checks.has_permissions(administrator=True)
async def zaplacono(interaction: discord.Interaction, order_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        order_data = supabase.table("orders").select("*").eq("id", order_id).execute().data
        if not order_data:
            await interaction.followup.send("❌ Nie znaleziono zamówienia.", ephemeral=True)
            return
        order = order_data[0]

        if order["status"] == "completed":
            await interaction.followup.send("⚠️ To zamówienie jest już zrealizowane.", ephemeral=True)
            return

        account_data = supabase.table("steam_accounts").select("*").eq("id", order["product_id"]).execute().data
        if not account_data:
            await interaction.followup.send("❌ Brak konta w bazie.", ephemeral=True)
            return
        account = account_data[0]

        supabase.table("steam_accounts").update({"sold": True}).eq("id", account["id"]).execute()
        supabase.table("orders").update({"status": "completed"}).eq("id", order_id).execute()

        await interaction.followup.send(
            f"✅ Gotowe! Konto **{account.get('name')}** odblokowane dla <@{order['discord_id']}>.",
            ephemeral=True
        )

        channel_id = order.get("channel_id")
        target_channel = bot.get_channel(int(channel_id)) if channel_id else interaction.channel

        if target_channel:
            done_embed = discord.Embed(
                title="✅ Płatność potwierdzona!",
                description=(
                    f"<@{order['discord_id']}> Twoje zamówienie zostało zrealizowane!\n\n"
                    "**Jak odebrać dane:**\n"
                    "1. Wejdź na stronę sklepu\n"
                    "2. Kliknij **Moje zamówienia**\n"
                    "3. Znajdziesz tam login, hasło i instrukcję logowania"
                ),
                color=0x22c55e,
            )
            done_embed.set_footer(text="Arcyn · Kanał zostanie usunięty za 20 sekund")
            await target_channel.send(f"<@{order['discord_id']}>", embed=done_embed)
            await asyncio.sleep(20)
            await target_channel.delete(reason="Zamówienie zakończone")

    except Exception as e:
        await interaction.followup.send(f"❌ Błąd: {e}", ephemeral=True)


@bot.tree.command(name="anuluj", description="Anuluj zamówienie i zamknij ticket")
@app_commands.describe(order_id="ID zamówienia", powod="Powód anulowania (opcjonalnie)")
@app_commands.checks.has_permissions(administrator=True)
async def anuluj(interaction: discord.Interaction, order_id: str, powod: str = "Brak podanego powodu"):
    await interaction.response.defer(ephemeral=True)
    try:
        order_data = supabase.table("orders").select("*").eq("id", order_id).execute().data
        if not order_data:
            await interaction.followup.send("❌ Nie znaleziono zamówienia.", ephemeral=True)
            return
        order = order_data[0]

        supabase.table("orders").update({"status": "cancelled"}).eq("id", order_id).execute()
        await interaction.followup.send(f"✅ Zamówienie anulowane. Powód: {powod}", ephemeral=True)

        channel_id = order.get("channel_id")
        target_channel = bot.get_channel(int(channel_id)) if channel_id else interaction.channel

        if target_channel:
            cancel_embed = discord.Embed(
                title="❌ Zamówienie anulowane",
                description=f"<@{order['discord_id']}> Twoje zamówienie zostało anulowane.\n**Powód:** {powod}\n\nJeśli masz pytania, skontaktuj się z administracją.",
                color=0xef4444,
            )
            cancel_embed.set_footer(text="Arcyn · Kanał zostanie usunięty za 15 sekund")
            await target_channel.send(f"<@{order['discord_id']}>", embed=cancel_embed)
            await asyncio.sleep(15)
            await target_channel.delete(reason="Zamówienie anulowane")

    except Exception as e:
        await interaction.followup.send(f"❌ Błąd: {e}", ephemeral=True)


@bot.tree.command(name="lista", description="Pokaż wszystkie oczekujące zamówienia")
@app_commands.checks.has_permissions(administrator=True)
async def lista(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        orders = supabase.table("orders").select("*, steam_accounts(name, price)").in_("status", ["pending", "awaiting_payment"]).order("created_at").execute().data or []

        if not orders:
            await interaction.followup.send("✅ Brak oczekujących zamówień.", ephemeral=True)
            return

        embed = discord.Embed(title=f"📋 Oczekujące zamówienia ({len(orders)})", color=0x3b82f6)
        for o in orders[:20]:
            product_name = (o.get("steam_accounts") or {}).get("name", "?")
            price = (o.get("steam_accounts") or {}).get("price", "?")
            status_icon = "⏳" if o["status"] == "awaiting_payment" else "🕐"
            embed.add_field(
                name=f"{status_icon} {product_name} — {price} PLN",
                value=f"ID: `{o['id'][:16]}...`\nKlient: <@{o['discord_id']}>\nStatus: `{o['status']}`",
                inline=False
            )
        if len(orders) > 20:
            embed.set_footer(text=f"Pokazano 20 z {len(orders)} zamówień")

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Błąd: {e}", ephemeral=True)


@bot.tree.command(name="statystyki", description="Pokaż statystyki sklepu Arcyn")
@app_commands.checks.has_permissions(administrator=True)
async def statystyki(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        products = supabase.table("steam_accounts").select("price, sold").execute().data or []
        orders = supabase.table("orders").select("status").execute().data or []

        total = len(products)
        available = sum(1 for p in products if not p.get("sold"))
        sold = sum(1 for p in products if p.get("sold"))
        revenue = sum((p.get("price") or 0) for p in products if p.get("sold"))

        pending = sum(1 for o in orders if o["status"] in ["pending", "awaiting_payment"])
        completed = sum(1 for o in orders if o["status"] == "completed")
        cancelled = sum(1 for o in orders if o["status"] == "cancelled")

        embed = discord.Embed(
            title="📊 Statystyki — Arcyn",
            color=0xa78bfa,
        )
        embed.add_field(name="📦 Produkty łącznie", value=f"**{total}**", inline=True)
        embed.add_field(name="✅ Dostępne", value=f"**{available}**", inline=True)
        embed.add_field(name="💸 Sprzedane", value=f"**{sold}**", inline=True)
        embed.add_field(name="💰 Przychód", value=f"**{revenue:.2f} PLN**", inline=True)
        embed.add_field(name="⏳ Oczekujące", value=f"**{pending}**", inline=True)
        embed.add_field(name="✅ Zrealizowane", value=f"**{completed}**", inline=True)
        embed.add_field(name="❌ Anulowane", value=f"**{cancelled}**", inline=True)
        embed.set_footer(text=f"Arcyn · {datetime.now().strftime('%d.%m.%Y %H:%M')}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Błąd: {e}", ephemeral=True)


@bot.tree.command(name="znajdz", description="Znajdź zamówienia użytkownika po Discord ID")
@app_commands.describe(discord_id="Discord ID użytkownika")
@app_commands.checks.has_permissions(administrator=True)
async def znajdz(interaction: discord.Interaction, discord_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        orders = supabase.table("orders").select("*, steam_accounts(name, price)").eq("discord_id", discord_id).order("created_at", desc=True).limit(10).execute().data or []

        if not orders:
            await interaction.followup.send(f"❌ Brak zamówień dla użytkownika `{discord_id}`.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🔍 Zamówienia użytkownika",
            description=f"<@{discord_id}> · ID: `{discord_id}`",
            color=0xf59e0b,
        )
        status_icons = {
            "completed": "✅",
            "awaiting_payment": "⏳",
            "pending": "🕐",
            "cancelled": "❌",
        }
        for o in orders:
            product_name = (o.get("steam_accounts") or {}).get("name", "?")
            price = (o.get("steam_accounts") or {}).get("price", "?")
            icon = status_icons.get(o["status"], "❓")
            date = o.get("created_at", "")[:10]
            embed.add_field(
                name=f"{icon} {product_name} — {price} PLN",
                value=f"ID: `{o['id'][:20]}...`\nStatus: `{o['status']}` · Data: {date}",
                inline=False
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Błąd: {e}", ephemeral=True)


@bot.tree.command(name="info", description="Pokaż info o zamówieniu na tym kanale (lub po ID)")
@app_commands.describe(order_id="ID zamówienia (opcjonalnie — jeśli pusty, szuka po kanale)")
@app_commands.checks.has_permissions(administrator=True)
async def info(interaction: discord.Interaction, order_id: str = None):
    await interaction.response.defer(ephemeral=True)
    try:
        if order_id:
            orders = supabase.table("orders").select("*, steam_accounts(name, price, login, password)").eq("id", order_id).execute().data
        else:
            channel_id = str(interaction.channel.id)
            orders = supabase.table("orders").select("*, steam_accounts(name, price, login, password)").eq("channel_id", channel_id).execute().data

        if not orders:
            await interaction.followup.send("❌ Nie znaleziono zamówienia.", ephemeral=True)
            return

        o = orders[0]
        account = o.get("steam_accounts") or {}
        status_icons = {"completed": "✅", "awaiting_payment": "⏳", "pending": "🕐", "cancelled": "❌"}
        icon = status_icons.get(o["status"], "❓")

        embed = discord.Embed(
            title=f"{icon} Zamówienie — {account.get('name', '?')}",
            color=0x3b82f6,
        )
        embed.add_field(name="🆔 ID", value=f"`{o['id']}`", inline=False)
        embed.add_field(name="👤 Klient", value=f"<@{o['discord_id']}>", inline=True)
        embed.add_field(name="💰 Cena", value=f"{account.get('price', '?')} PLN", inline=True)
        embed.add_field(name="📊 Status", value=f"`{o['status']}`", inline=True)
        if o["status"] == "completed":
            embed.add_field(name="🔑 Login", value=f"`{account.get('login', '?')}`", inline=True)
            embed.add_field(name="🔒 Hasło", value=f"`{account.get('password', '?')}`", inline=True)
        date = o.get("created_at", "")[:10]
        embed.set_footer(text=f"Arcyn · Zamówiono: {date}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Błąd: {e}", ephemeral=True)


@bot.tree.command(name="ogloszenie", description="Wyślij ogłoszenie jako embed do wybranego kanału")
@app_commands.describe(kanal="Kanał docelowy", tytul="Tytuł ogłoszenia", tresc="Treść ogłoszenia", kolor="Kolor: niebieski / zielony / czerwony / zloty")
@app_commands.choices(kolor=[
    app_commands.Choice(name="Niebieski", value="blue"),
    app_commands.Choice(name="Zielony", value="green"),
    app_commands.Choice(name="Czerwony", value="red"),
    app_commands.Choice(name="Złoty", value="gold"),
])
@app_commands.checks.has_permissions(administrator=True)
async def ogloszenie(interaction: discord.Interaction, kanal: discord.TextChannel, tytul: str, tresc: str, kolor: str = "blue"):
    await interaction.response.defer(ephemeral=True)
    colors = {"blue": 0x3b82f6, "green": 0x22c55e, "red": 0xef4444, "gold": 0xf59e0b}
    embed = discord.Embed(title=tytul, description=tresc, color=colors.get(kolor, 0x3b82f6))
    embed.set_footer(text=f"Arcyn · {interaction.user.display_name}")
    try:
        await kanal.send(embed=embed)
        await interaction.followup.send(f"✅ Ogłoszenie wysłane na {kanal.mention}!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Błąd: {e}", ephemeral=True)


@bot.tree.command(name="zamknij", description="Ręcznie zamknij i usuń ten kanał ticket")
@app_commands.checks.has_permissions(administrator=True)
async def zamknij(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    embed = discord.Embed(
        title="🔒 Kanał zostanie zamknięty",
        description=f"Ticket zamknięty przez {interaction.user.mention}.\nKanał zostanie usunięty za 10 sekund.",
        color=0x6b7280,
    )
    await channel.send(embed=embed)
    await interaction.followup.send("✅ Zamykam kanał...", ephemeral=True)
    await asyncio.sleep(10)
    try:
        await channel.delete(reason=f"Ręcznie zamknięty przez {interaction.user}")
    except Exception as e:
        print(f"[Bot] Błąd zamknięcia kanału: {e}")


@bot.event
async def on_ready():
    print(f"✅ Bot zalogowany jako {bot.user}")
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    print("✅ Slash commands zsynchronizowane")
    bot.loop.create_task(poll_orders())


bot.run(DISCORD_TOKEN)
