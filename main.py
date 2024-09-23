import dns.resolver
import asyncio
import configparser
import os
import time
from dns.exception import DNSException, Timeout
from dns.resolver import NoNameservers, NXDOMAIN
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import multiprocessing
import logging
from rich.console import Console
from rich.progress import track

# Создание консоли для вывода через rich
console = Console()

# --- Настройка и чтение конфигурации ---
def load_config():
    config = configparser.ConfigParser()
    config.read('config.ini')
    return config

config = load_config()
domains = [domain.strip() for domain in config['settings']['domains'].split(',')]
nameservers_file = config['settings']['nameservers_file']
subdomains_file = config['settings']['subdomains_file']
timeout = int(config['settings']['timeout'])
output_directory = config['settings']['output_directory']
batch_size = 100  # Размер пакета
debug = config.getboolean('settings', 'debug', fallback=False)  # Параметр отладки

# Получаем количество доступных ядер процессора
available_threads = multiprocessing.cpu_count() * 2  # Удвоим для максимальной нагрузки

# Количество потоков устанавливаем из конфигурации или используем максимальное
threads = int(config.get('settings', 'threads', fallback=available_threads))

# --- Логирование в файл для отладки ---
def setup_logging(debug_mode):
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    log_file = 'debug.log'
    if debug_mode:
        logging.basicConfig(level=logging.DEBUG, format=log_format, filename=log_file, filemode='w')
        logging.debug("Режим отладки включен. Логирование записывается в debug.log.")
    else:
        logging.basicConfig(level=logging.WARNING, format=log_format)

setup_logging(debug)

# --- Вспомогательные функции для работы с файлами и IP ---
def is_valid_ip(ip):
    parts = ip.split(".")
    return len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)

def load_nameservers(nameservers_file):
    with open(nameservers_file, 'r') as ns_file:
        return [line.strip() for line in ns_file.readlines() if line.strip() and is_valid_ip(line.strip())]

def load_subdomains(subdomains_file):
    with open(subdomains_file, 'r') as sub_file:
        return [line.strip() for line in sub_file.readlines()]

nameservers = load_nameservers(nameservers_file)
subdomains = load_subdomains(subdomains_file)

# --- Функция проверки валидности DNS-сервера ---
def validate_nameserver(nameserver):
    resolver = dns.resolver.Resolver()
    resolver.nameservers = [nameserver]
    try:
        logging.debug(f"Проверка DNS-сервера: {nameserver}")
        resolver.resolve("google.com", 'A', lifetime=timeout)
        return nameserver  # Возвращаем валидный сервер
    except (DNSException, Timeout):
        logging.debug(f"DNS-сервер {nameserver} недоступен")
        return None  # Сервер невалидный

# --- Функции обработки поддоменов ---
async def check_subdomain(nameserver, domain, subdomain):
    resolver = dns.resolver.Resolver()
    resolver.nameservers = [nameserver]
    full_subdomain = f"{subdomain}.{domain}"
    try:
        # Проверка A-записи
        logging.debug(f"Проверка поддомена: {full_subdomain}")
        await asyncio.to_thread(resolver.resolve, full_subdomain, 'A', lifetime=timeout)

        # Проверка MX-записи
        try:
            await asyncio.to_thread(resolver.resolve, full_subdomain, 'MX', lifetime=timeout)
        except (NXDOMAIN, NoNameservers, DNSException):
            pass

        # Проверка CNAME-записи
        try:
            await asyncio.to_thread(resolver.resolve, full_subdomain, 'CNAME', lifetime=timeout)
        except (NXDOMAIN, NoNameservers, DNSException):
            pass

        return full_subdomain
    except (NXDOMAIN, NoNameservers, DNSException):
        logging.debug(f"Поддомен {full_subdomain} не найден")
        return None

# --- Обработка батчей и поддоменов ---
async def process_batch(domain, valid_nameservers, subdomains_batch, output_file):
    unique_subdomains = set()  # Множество для хранения уникальных поддоменов
    sem = asyncio.Semaphore(threads)  # Ограничиваем количество одновременно выполняемых задач

    tasks = []
    with ThreadPoolExecutor(max_workers=threads) as executor:
        async def task_wrapper(nameserver, subdomain):
            async with sem:
                result = await check_subdomain(nameserver, domain, subdomain)
                if result:
                    unique_subdomains.add(result)  # Добавляем уникальный поддомен

        for nameserver in valid_nameservers:
            for subdomain in subdomains_batch:
                tasks.append(task_wrapper(nameserver, subdomain))

        await asyncio.gather(*tasks)

    # Записываем уникальные поддомены в файл после обработки батча
    with open(output_file, 'a') as f:
        for subdomain in unique_subdomains:
            f.write(subdomain + '\n')
    logging.debug(f"Записан результат батча в файл {output_file}")

async def process_domain(domain, valid_nameservers):
    output_file = os.path.join(output_directory, f"{domain}.txt")

    # Разбиваем поддомены на батчи
    subdomain_batches = [subdomains[i:i + batch_size] for i in range(0, len(subdomains), batch_size)]

    # Используем tqdm для прогресса и оценки времени
    for batch_num, subdomains_batch in enumerate(tqdm(subdomain_batches, desc=f"Обработка домена {domain}"), start=1):
        await process_batch(domain, valid_nameservers, subdomains_batch, output_file)

# --- Основная функция ---
async def main():
    # Проверка валидности всех DNS-серверов
    console.print("[bold blue]Проверка валидности DNS-серверов...[/bold blue]")
    valid_nameservers = []
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {executor.submit(validate_nameserver, nameserver): nameserver for nameserver in nameservers}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Проверка серверов", unit=" серверов"):
            result = future.result()
            if result:
                valid_nameservers.append(result)

    if not valid_nameservers:
        console.print("[bold red]Нет валидных DNS-серверов.[/bold red]")
        return

    # Обработка каждого домена с валидными nameservers
    for domain in domains:
        console.print(f"[bold green]Начало обработки домена: {domain}[/bold green]")
        await process_domain(domain, valid_nameservers)

if __name__ == "__main__":
    asyncio.run(main())
