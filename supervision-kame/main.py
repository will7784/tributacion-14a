import sys
import os
import re
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich import print as rprint
from rich import box
from datetime import datetime

from app.parsers.kame import read_kame_excel
from app.parsers.softland import read_softland_excel
from app.core.db import save_to_ledger, run_query

console = Console()

# Estado de la sesión global
session = {
    "user": None,
    "erp": None,
    "db_name": None,
    "empresa": "No seleccionada",
    "rut": ""
}

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def show_welcome():
    clear_screen()
    header_text = f"[bold cyan]P Y - A N A L Y T I C A - C F O[/bold cyan]\n"
    header_text += f"[dim]Supervisión Contable y Estrategia Financiera[/dim]\n"
    
    if session["db_name"]:
        header_text += f"\n[bold green]Empresa:[/bold green] {session['empresa']} [dim]({session['rut']})[/dim]"
        header_text += f"\n[bold yellow]ERP:[/bold yellow] {session['erp']}"
    
    console.print(Panel.fit(header_text, border_style="cyan"))

def login_flow():
    show_welcome()
    console.print("[bold yellow]>>> ACCESO AL SISTEMA[/bold yellow]\n")
    user = Prompt.ask("Usuario")
    # En un entorno real usaríamos getpass o similar para el password
    Prompt.ask("Contraseña", password=True)
    session["user"] = user

def select_erp_flow():
    show_welcome()
    console.print("[bold yellow]>>> SELECCIONAR ERP[/bold yellow]\n")
    console.print("[1] KAME ERP")
    console.print("[2] SOFTLAND")
    choice = Prompt.ask("Selecciona ERP", choices=["1", "2"])
    session["erp"] = "KAME" if choice == "1" else "SOFTLAND"

def get_db_metadata(db_name):
    """Intenta leer el nombre y RUT de la empresa desde la base de datos."""
    try:
        # Intentamos ver si existe una tabla de metadata
        res = run_query("SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'", db_name)
        if res is not None and not res.empty:
            meta = run_query("SELECT empresa_nombre, empresa_rut FROM metadata LIMIT 1", db_name)
            if meta is not None and not meta.empty:
                return meta.iloc[0]['empresa_nombre'], meta.iloc[0]['empresa_rut']
    except:
        pass
    return None, None

def set_db_metadata(db_name, nombre, rut):
    """Guarda el nombre y RUT de la empresa en la base de datos."""
    import sqlite3
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS metadata (empresa_nombre TEXT, empresa_rut TEXT)")
    cursor.execute("DELETE FROM metadata")
    cursor.execute("INSERT INTO metadata (empresa_nombre, empresa_rut) VALUES (?, ?)", (nombre, rut))
    conn.commit()
    conn.close()

def clean_path(path):
    """Limpia rutas de archivos arrastradas desde PowerShell/Windows."""
    path = path.strip()
    if path.startswith('& '):
        path = path[2:].strip()
    # Eliminar comillas externas e internas de forma iterativa si es necesario
    while (path.startswith("'") and path.endswith("'")) or (path.startswith('"') and path.endswith('"')):
        path = path[1:-1].strip()
    return path


def rut_to_kame_db_name(rut):
    clean = re.sub(r"[^0-9Kk]", "", str(rut or "")).upper()
    return f"{clean}_kame.db" if clean else "empresa_kame.db"


def get_known_companies():
    """Busca en todas las DBs disponibles para crear un catálogo de empresas conocidas."""
    companies = {} # rut -> nombre
    db_files = [f for f in os.listdir('.') if f.endswith('_kame.db')]
    for db in db_files:
        nombre, rut = get_db_metadata(db)
        if rut and nombre:
            companies[rut] = nombre
    return companies

def select_company_flow():
    show_welcome()
    erp = session["erp"]
    console.print(f"[bold yellow]>>> SELECCIONAR EMPRESA Y PERÍODO ({erp})[/bold yellow]\n")
    
    if erp == "KAME":
        db_files = sorted([f for f in os.listdir('.') if f.endswith('_kame.db')], reverse=True)
    else:
        db_files = sorted([f for f in os.listdir('.') if f.endswith('_softland.db')], reverse=True)
    
    if db_files:
        table = Table(title=f"Historial de Empresas: {erp}", expand=True, box=box.SIMPLE_HEAD)
        table.add_column("ID", justify="right", style="cyan", width=4)
        table.add_column("Empresa", style="bold green", ratio=3)
        table.add_column("RUT", style="dim", ratio=2)
        table.add_column("Archivo / Período", style="white", ratio=3)
        
        for i, f in enumerate(db_files):
            nombre, rut = get_db_metadata(f)
            table.add_row(str(i+1), nombre or "Nueva (Sin asignar)", rut or "-", f)
        
        console.print(table)
        choices = [str(i+1) for i in range(len(db_files))] + ["N", "n"]
        idx = Prompt.ask("Selecciona ID o 'N' para importar nueva", choices=choices)
        
        if idx.lower() == 'n':
            import_flow(session["erp"], read_kame_excel if session["erp"] == "KAME" else read_softland_excel)
            return select_company_flow()
            
        session["db_name"] = db_files[int(idx)-1]
        nombre, rut = get_db_metadata(session["db_name"])
        
        if not nombre:
            known = get_known_companies()
            console.print("\n[yellow]Esta base de datos no tiene información de la empresa.[/yellow]")
            rut = Prompt.ask("RUT de la Empresa")
            sug_nombre = known.get(rut, "")
            nombre = Prompt.ask("Nombre de la Empresa", default=sug_nombre)
            set_db_metadata(session["db_name"], nombre, rut)
            
        session["empresa"] = nombre
        session["rut"] = rut
    else:
        # ... (resto igual)
        console.print("[yellow]No hay empresas registradas. Debes importar una.[/yellow]")
        if Confirm.ask("¿Deseas importar un archivo Excel ahora?"):
            import_flow(session["erp"], read_kame_excel if session["erp"] == "KAME" else read_softland_excel)
            return select_company_flow()
        else:
            return select_erp_flow()

def import_flow(erp_name, parser_func):
    show_welcome()
    console.print(f"[bold yellow]>>> IMPORTAR NUEVO LEDGER ({erp_name})[/bold yellow]\n")
    
    file_path_raw = Prompt.ask("Arrastra el archivo Excel aquí o escribe la ruta")
    file_path = clean_path(file_path_raw)
    
    if not os.path.exists(file_path):
        console.print(f"[red]Error: El archivo no existe en:[/red] [cyan]{file_path}[/cyan]")
        Prompt.ask("\nPresiona Enter para volver")
        return

    try:
        known = get_known_companies()
        rut_emp = Prompt.ask("RUT de la Empresa")
        sug_nombre = known.get(rut_emp, "")
        nombre_emp = Prompt.ask("Nombre de la Empresa", default=sug_nombre)
        
        with console.status(f"[bold green]Procesando Excel {erp_name}...") as status:
            df = parser_func(file_path)
            if erp_name == "KAME":
                db_name = rut_to_kame_db_name(rut_emp)
                if db_name == "empresa_kame.db":
                    raise ValueError("RUT inválido para nombre de base de datos.")
            else:
                db_name = Path(file_path).stem + f"_{erp_name.lower()}.db"
            save_to_ledger(df, db_name)
            set_db_metadata(db_name, nombre_emp, rut_emp)
            
        console.print(f"\n[bold green]✓ Éxito![/bold green] Empresa [cyan]{nombre_emp}[/cyan] registrada.")
        Prompt.ask("\nPresiona Enter para continuar")
        
    except Exception as e:
        console.print(f"\n[bold red]Error durante la importación:[/bold red] {e}")
        Prompt.ask("\nPresiona Enter para volver")

def report_flow():
    if not session["db_name"]:
        console.print("[red]Error: No has seleccionado una empresa.[/red]")
        Prompt.ask("\nPresiona Enter para continuar")
        return

    show_welcome()
    console.print("[bold yellow]>>> GENERAR REPORTES[/bold yellow]\n")
    
    console.print("[1] Análisis Inteligente (Solo Pendientes de Pago/Cobro)")
    console.print("[2] Histórico Completo (Todos los movimientos con status)")
    console.print("[3] Comprobante Contable (Ver el asiento completo)")
    console.print("[4] Balance 8 Columnas (Estado de Situación y Resultados)")
    console.print("[5] Libro Mayor (Detalle por cuenta con glosas)")
    tipo_choice = Prompt.ask("Selecciona tipo", choices=["1", "2", "3", "4", "5"])
    
    mapping = {"1": "inteligente", "2": "historico", "3": "comprobante", "4": "balance", "5": "mayor"}
    tipo_analisis = mapping[tipo_choice]

    busqueda = ""
    fecha_balance = ""
    
    if tipo_analisis == "balance":
        fecha_input = Prompt.ask("\n¿Hasta qué fecha quieres el balance? (DD-MM-AAAA, ej: 31-12-2025)")
        try:
            fecha_balance = datetime.strptime(fecha_input, "%d-%m-%Y").strftime("%Y-%m-%d")
        except:
            console.print("[bold red]Formato de fecha inválido. Usando fecha actual.[/bold red]")
            fecha_balance = datetime.now().strftime("%Y-%m-%d")
    else:
        msg = "\n¿Qué quieres buscar? (Nombre/RUT, o '*' para todas): " if tipo_analisis == "mayor" else "\n¿Qué quieres buscar? (En 1 y 2: nombre/rut, En 3: e338, i10, etc.): "
        busqueda = Prompt.ask(msg)

    try:
        from app.reports.excel import export_balance_8_columnas, export_report_to_excel
        db_name = session["db_name"]
        archivo = None
        
        if tipo_analisis == "balance":
            query = """
            SELECT cuenta, nombre_cuenta, SUM(debe) as debe, SUM(haber) as haber
            FROM ledger WHERE fecha <= ? GROUP BY cuenta, nombre_cuenta ORDER BY cuenta
            """
            df_bal = run_query(query, db_name, params=[fecha_balance])
            
            if not df_bal.empty:
                table = Table(title=f"Balance Tributario al {fecha_input}", expand=True, box=box.ROUNDED)
                table.add_column("Cuenta - Nombre", style="yellow", ratio=8)
                table.add_column("Debe", justify="right", style="cyan")
                table.add_column("Haber", justify="right", style="cyan")
                table.add_column("Activo", justify="right", style="green")
                table.add_column("Pasivo", justify="right", style="magenta")
                table.add_column("Pérdida", justify="right", style="red")
                table.add_column("Ganancia", justify="right", style="blue")
                
                t_debe = t_haber = t_activo = t_pasivo = t_perdida = t_ganancia = 0
                for _, row in df_bal.iterrows():
                    d, h = float(row['debe']), float(row['haber'])
                    neto = d - h
                    digit = str(row['cuenta'])[0]
                    a = p = per = gan = 0
                    if digit in ['1', '2']:
                        if neto >= 0: a = neto
                        else: p = abs(neto)
                    else:
                        if neto >= 0: per = neto
                        else: gan = abs(neto)
                    t_debe += d; t_haber += h
                    t_activo += a; t_pasivo += p
                    t_perdida += per; t_ganancia += gan
                    table.add_row(f"{row['cuenta']} - {row['nombre_cuenta']}", f"{d:,.0f}".replace(",", "."), f"{h:,.0f}".replace(",", "."), f"{a:,.0f}".replace(",", "."), f"{p:,.0f}".replace(",", "."), f"{per:,.0f}".replace(",", "."), f"{gan:,.0f}".replace(",", "."))
                
                table.add_section()
                table.add_row("SUMAS TOTALES", f"{t_debe:,.0f}".replace(",", "."), f"{t_haber:,.0f}".replace(",", "."), f"{t_activo:,.0f}".replace(",", "."), f"{t_pasivo:,.0f}".replace(",", "."), f"{t_perdida:,.0f}".replace(",", "."), f"{t_ganancia:,.0f}".replace(",", "."), style="bold")
                
                ub, ur = t_activo - t_pasivo, t_ganancia - t_perdida
                ra, rp, rper, rgan = (max(0, -ub) if ub < 0 else 0), (max(0, ub) if ub > 0 else 0), (max(0, ur) if ur > 0 else 0), (max(0, -ur) if ur < 0 else 0)
                table.add_row("RESULTADO DEL EJERCICIO", "", "", f"{ra:,.0f}".replace(",", ".") if ra > 0 else "", f"{rp:,.0f}".replace(",", ".") if rp > 0 else "", f"{rper:,.0f}".replace(",", ".") if rper > 0 else "", f"{rgan:,.0f}".replace(",", ".") if rgan > 0 else "", style="bold yellow")
                table.add_row("TOTALES IGUALES", f"{t_debe:,.0f}".replace(",", "."), f"{t_haber:,.0f}".replace(",", "."), f"{t_activo + ra:,.0f}".replace(",", "."), f"{t_pasivo + rp:,.0f}".replace(",", "."), f"{t_perdida + rper:,.0f}".replace(",", "."), f"{t_ganancia + rgan:,.0f}".replace(",", "."), style="bold green")
                
                show_welcome()
                console.print(table)
                if Confirm.ask("\n¿Deseas exportar el Balance completo de 8 columnas a Excel?"):
                    with console.status("[bold green]Generando Excel..."):
                        archivo = export_balance_8_columnas(db_name, fecha_balance)
            else:
                console.print("[yellow]No hay datos para la fecha seleccionada.[/yellow]")

        elif tipo_analisis == "comprobante":
            terminos = [t.strip() for t in busqueda.split(",") if t.strip()]
            params = [f"%{t}" for t in terminos]
            filtros = " OR ".join(["comprobante LIKE ?" for _ in terminos])
            query = f"SELECT fecha, comprobante, cuenta, nombre_cuenta, ficha, razon_social, documento, debe, haber, concepto, COALESCE(proyecto, '') as proyecto FROM ledger WHERE {filtros} ORDER BY comprobante, cuenta"
            df = run_query(query, db_name, params=params)
            
            if df is not None and not df.empty:
                table = Table(title=f"Asiento Contable: {busqueda}", expand=True, box=box.ROUNDED)
                table.add_column("Fecha", style="dim", no_wrap=True)
                table.add_column("Cuenta - Nombre", style="yellow", ratio=3) 
                table.add_column("Ficha", style="magenta", ratio=3)
                table.add_column("Documento", style="blue", ratio=3)
                table.add_column("Proyecto", style="green", ratio=3)
                table.add_column("Debe", justify="right", style="bold green", no_wrap=True)
                table.add_column("Haber", justify="right", style="bold red", no_wrap=True)
                table.add_column("Glosa", style="dim", ratio=3)
                
                td, th = 0, 0
                for _, row in df.iterrows():
                    d, h = float(row['debe']), float(row['haber'])
                    td += d; th += h
                    cn = f"{row['cuenta']} - {row['nombre_cuenta']}"
                    table.add_row(str(row['fecha']), cn, str(row['ficha']) if row['ficha'] else "", str(row['documento']) if row['documento'] else "", str(row['proyecto']) if row['proyecto'] else "", f"{d:,.0f}".replace(",", "."), f"{h:,.0f}".replace(",", "."), str(row['concepto'])[:100])
                table.add_section()
                table.add_row("TOTAL", "", "", "", "", f"{td:,.0f}".replace(",", "."), f"{th:,.0f}".replace(",", "."), "", style="bold")
                show_welcome()
                console.print(table)
            else:
                console.print(f"[yellow]No se encontró el comprobante '{busqueda}'.[/yellow]")
        elif tipo_analisis == "mayor":
            with console.status(f"[bold green]Generando Libro Mayor...") as status:
                from app.reports.excel import export_libro_mayor
                archivo = export_libro_mayor(db_name, busqueda)
        else:
            with console.status(f"[bold green]Generando {tipo_analisis}...") as status:
                archivo = export_report_to_excel(db_name, busqueda, tipo_analisis=tipo_analisis)
            
        if archivo:
            console.print(f"\n[bold green]✓ Reporte Excel generado:[/bold green] [cyan]{archivo}[/cyan]")
        else:
            console.print(f"\n[bold yellow]⚠ No se encontró información para:[/bold yellow] [cyan]'{busqueda}'[/cyan]")
    except Exception as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
    Prompt.ask("\nPresiona Enter para volver")

def main_menu():
    if not session["user"]:
        login_flow()
    if not session["erp"]:
        select_erp_flow()
    if not session["db_name"]:
        select_company_flow()
        
    while True:
        show_welcome()
        console.print("[1] Generar Reportes")
        console.print("[2] Importar Nuevo Ledger")
        console.print("[3] Cambiar Empresa / ERP")
        console.print("[4] Salir")
        
        choice = Prompt.ask("Selecciona una opción", choices=["1", "2", "3", "4"])
        
        if choice == "1":
            report_flow()
        elif choice == "2":
            import_flow(session["erp"], read_kame_excel if session["erp"] == "KAME" else read_softland_excel)
        elif choice == "3":
            session["db_name"] = None
            session["erp"] = None
            select_erp_flow()
            select_company_flow()
        else:
            console.print("\n[bold cyan]¡Hasta pronto CFO![/bold cyan]")
            break

if __name__ == "__main__":
    main_menu()
