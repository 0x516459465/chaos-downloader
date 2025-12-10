#!/usr/bin/env python3
import os
import urllib.request
import urllib.parse
import json
import zipfile
import sqlite3
import threading
import concurrent.futures
import platform
from pathlib import Path

import questionary
from tabulate import tabulate
import colorama
from tqdm import tqdm

# Initialize colorama
colorama.init(autoreset=True)

class ChaosDownloader:
    def __init__(self):
        self.Red = colorama.Fore.RED
        self.Green = colorama.Fore.GREEN
        self.White = colorama.Fore.WHITE
        self.Yellow = colorama.Fore.YELLOW
        self.Reset = colorama.Style.RESET_ALL

        self.db_lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.setup_database()

        # Load data immediately
        try:
            self.data_json = self.load_data()
        except Exception as e:
            print(f"{self.Red}Error loading data from API: {e}{self.Reset}")
            self.data_json = []

        # Setup opener for urlretrieve
        opener = urllib.request.build_opener()
        opener.addheaders = [('User-Agent', 'Mozilla/5.0')]
        urllib.request.install_opener(opener)

        # Menu Style
        self.style = questionary.Style([
            ('qmark', 'fg:red bold'),
            ('question', 'fg:red bold'),
            ('answer', 'fg:yellow'),
            ('pointer', 'fg:red bold'),
            ('highlighted', 'bg:red fg:yellow'),
            ('selected', 'fg:yellow'),
            ('separator', 'fg:#cc5454'),
            ('instruction', ''),
            ('text', ''),
            ('disabled', 'fg:#858585 italic')
        ])

    def clear_screen(self):
        os.system('cls' if os.name == 'nt' else 'clear')

    def setup_database(self):
        self.conn = sqlite3.connect('chaos.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        with self.db_lock:
            self.cursor.execute(
                "CREATE TABLE IF NOT EXISTS names (ID INTEGER PRIMARY KEY, name TEXT UNIQUE, platform TEXT, offer_bounty BOOLEAN, late_update DATE);"
            )
            self.cursor.execute(
                "CREATE TABLE IF NOT EXISTS subdomains (ID INTEGER PRIMARY KEY, subdomain TEXT UNIQUE, program_ID INTEGER, FOREIGN KEY(program_ID) REFERENCES names(ID));"
            )
            self.conn.commit()

    def load_data(self):
        url = "https://chaos-data.projectdiscovery.io/index.json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as response:
            webpage = response.read()
            return json.loads(webpage)

    def insert_table_name(self, program_name, platform, offer_bounty):
        with self.db_lock:
            try:
                self.cursor.execute(
                    "INSERT OR IGNORE INTO names(name, platform, offer_bounty, late_update) VALUES(?, ?, ?, DATE('NOW'));",
                    (program_name, platform, offer_bounty)
                )
                self.conn.commit()
            except Exception as e:
                print(f"DB error: {e}")

    def unzip_files(self, file_path, save_dir):
        try:
            with zipfile.ZipFile(file_path, 'r') as zf:
                zf.extractall(save_dir)
            file_path.unlink() # Delete zip file
        except Exception as e:
            print(f"Error unzipping {file_path}: {e}")

    def download(self, download_link, save_dir, file_name, program_name, platform, offer_bounty):
        program_dir = Path(save_dir) / program_name
        program_dir.mkdir(parents=True, exist_ok=True)

        self.insert_table_name(program_name, platform, offer_bounty)

        # Encode URL
        parsed = urllib.parse.urlsplit(download_link)
        encoded_path = urllib.parse.quote(parsed.path)
        download_link = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, encoded_path, parsed.query, parsed.fragment))

        file_path = program_dir / file_name
        try:
            urllib.request.urlretrieve(download_link, str(file_path))
            self.unzip_files(file_path, str(program_dir))
            print(f"{self.Red}[+]{self.White} {file_name} Done {self.Green}[\u2713]{self.Reset}")
        except Exception as e:
            print(f"Error downloading {file_name}: {e}")

    def merge_sub_files_and_insert(self, save_dir, program_name):
        program_dir = Path(save_dir) / program_name
        txt_files = list(program_dir.glob("*.txt"))
        all_subdomains = []

        # Read subdomains first to minimize lock time
        for file in txt_files:
            with file.open("r", encoding="utf-8") as infile:
                lines = infile.readlines()
                all_subdomains.extend([line.strip() for line in lines if line.strip()])

        # Critical section: Writing to shared files
        with self.file_lock:
            master_file = Path(f"{save_dir}.txt")
            with master_file.open("a", encoding="utf-8") as outfile:
                for sd in all_subdomains:
                    outfile.write(sd + "\n")

            # New subdomains file
            new_file_path = Path(f"new_{save_dir}.txt")
            with new_file_path.open("a", encoding="utf-8") as new_file:
                for sd in all_subdomains:
                    new_file.write(sd + "\n")

        # Batch insert (DB lock handled internally)
        with self.db_lock:
            self.cursor.execute("SELECT ID FROM names WHERE name=?", (program_name,))
            row = self.cursor.fetchone()
            if row:
                program_id = row[0]
                data_to_insert = [(sd, program_id) for sd in all_subdomains]
                self.cursor.executemany(
                    "INSERT OR IGNORE INTO subdomains(subdomain, program_ID) VALUES(?, ?)",
                    data_to_insert
                )
                self.conn.commit()

    def process_program(self, program, save_dir):
        # Extract filename from URL safely
        file_name = Path(urllib.parse.urlparse(program["URL"]).path).name
        program_name = program["name"]
        platform = program["platform"]
        bounty = program["bounty"]
        self.download(program["URL"], save_dir, file_name, program_name, platform, bounty)
        self.merge_sub_files_and_insert(save_dir, program_name)

    def download_filtered_programs(self, filter_func, save_dir):
        programs = [p for p in self.data_json if filter_func(p)]
        print(f"Starting download of {len(programs)} programs...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(self.process_program, prog, save_dir): prog for prog in programs}

            # Using tqdm to show progress bar
            for future in tqdm(concurrent.futures.as_completed(futures), total=len(programs), desc="Downloading", unit="prog"):
                prog = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    tqdm.write(f"{prog['name']} generated an exception: {exc}")

    # Filter Helpers
    def filter_by_platform(self, p, platform_name):
        # For "self hosted", platform is empty string
        target = platform_name.lower() if platform_name else ""
        return p["platform"].lower() == target

    # Menu Actions
    def action_download_all(self):
        self.download_filtered_programs(lambda p: True, "all_programmes")

    def action_offer_bounty(self):
        self.download_filtered_programs(lambda p: p["bounty"] is True, "offer_bounty")

    def action_not_offer_bounty(self):
        self.download_filtered_programs(lambda p: p["bounty"] is False, "not_offer_bounty")

    def select_platform(self):
        choices = ["Hackerone", "Bugcrowd", "Yeswehack", "Self hosted", "Back to Main Menu"]
        answer = questionary.select("Select Platform:", choices=choices, style=self.style).ask()
        if answer == "Back to Main Menu":
            return None
        return answer if answer != "Self hosted" else ""

    def action_platform(self):
        plat = self.select_platform()
        if plat is not None:
            dir_name = plat if plat else "self_hosted"
            self.download_filtered_programs(lambda p: self.filter_by_platform(p, plat), dir_name)

    def action_new_subdomain(self):
        self.download_filtered_programs(lambda p: p["change"] != 0, "new_subdomains")

    def action_new_subdomain_and_offer_bounty(self):
        self.download_filtered_programs(lambda p: p["change"] != 0 and p["bounty"] is True, "new_subdomains_and_offer_bounty")

    def action_new_subdomain_and_offer_bounty_and_platform(self):
        plat = self.select_platform()
        if plat is not None:
            dir_name = f"new_subdomain_and_offer_bounty_and_{plat}" if plat else "new_subdomain_and_offer_bounty_and_self_hosted"
            self.download_filtered_programs(lambda p: p["change"] != 0 and p["bounty"] is True and self.filter_by_platform(p, plat), dir_name)

    def action_new_subdomain_and_platform(self):
        plat = self.select_platform()
        if plat is not None:
            dir_name = f"new_subdomain_and_platform_{plat}" if plat else "new_subdomain_and_platform_self_hosted"
            self.download_filtered_programs(lambda p: p["change"] != 0 and self.filter_by_platform(p, plat), dir_name)

    def action_new_subdomain_and_not_offer_bounty(self):
        self.download_filtered_programs(lambda p: p["change"] != 0 and p["bounty"] is False, "new_subdomain_and_not_offer_bounty")

    def action_new_subdomain_and_not_offer_bounty_and_platform(self):
        plat = self.select_platform()
        if plat is not None:
            dir_name = f"new_subdomain_and_not_offer_bounty_and_{plat}" if plat else "new_subdomain_and_not_offer_bounty_and_self_hosted"
            self.download_filtered_programs(lambda p: p["change"] != 0 and p["bounty"] is False and self.filter_by_platform(p, plat), dir_name)

    def action_offer_bounty_and_platform(self):
        plat = self.select_platform()
        if plat is not None:
            dir_name = f"offer_bounty_and_{plat}" if plat else "offer_bounty_and_self_hosted"
            self.download_filtered_programs(lambda p: p["bounty"] is True and self.filter_by_platform(p, plat), dir_name)

    def action_not_offer_bounty_and_platform(self):
        plat = self.select_platform()
        if plat is not None:
            dir_name = f"not_offer_bounty_and_{plat}" if plat else "not_offer_bounty_and_self_hosted"
            self.download_filtered_programs(lambda p: p["bounty"] is False and self.filter_by_platform(p, plat), dir_name)

    def action_specific_program(self):
        prog_options = sorted([p["name"] for p in self.data_json])
        # Using checkbox for selection.
        selected_progs = questionary.checkbox(
            "Select Programs (Space to select, Enter to confirm):",
            choices=prog_options,
            style=self.style
        ).ask()

        if selected_progs:
            for prog_name in selected_progs:
                self.download_specific_program_logic(prog_name)

    def download_specific_program_logic(self, program_name):
        base_dir = program_name
        programs = [p for p in self.data_json if p["name"] == program_name]
        if programs:
            p = programs[0]
            info_table = [
                ["name", p['name']],
                ["program url", p['URL']],
                ["total subdomains", p['count']],
                ["new subdomains", p['change']],
                ["is new", p['is_new']],
                ["platform", p['platform']],
                ["offer reward", p['bounty']],
                ["last_updated", p['last_updated'][:10]]
            ]
            print(tabulate(info_table, headers=["Info", program_name], tablefmt="double_grid"))
            self.download_filtered_programs(lambda p: p["name"] == program_name, base_dir)
        else:
            print("Program not found.")

    def action_info(self):
        if not self.data_json:
            print("No data available.")
            return

        last_update = self.data_json[0]['last_updated'][:10]
        total_subdomains = sum(p["count"] for p in self.data_json)
        programs_changed = sum(1 for p in self.data_json if p["change"] != 0)
        new_programs = sum(1 for p in self.data_json if p["is_new"] == True)
        hackerone_programs = sum(1 for p in self.data_json if p["platform"].lower() == "hackerone")
        bugcrowd_programs = sum(1 for p in self.data_json if p["platform"].lower() == "bugcrowd")
        yeswehacker_programs = sum(1 for p in self.data_json if p["platform"].lower() == "yeswehack")
        self_hosted_programs = sum(1 for p in self.data_json if p["platform"] == "")
        programs_with_rewards = sum(1 for p in self.data_json if p["bounty"] == True)
        programs_with_no_rewards = sum(1 for p in self.data_json if p["bounty"] == False)
        programs_with_swag = sum(1 for p in self.data_json if 'swag' in p)

        info_options = [
            f"Programs last updated in {last_update}",
            f"{total_subdomains} Subdomains.",
            f"{len(self.data_json)} Programs.",
            f"{programs_changed} Programs changed.",
            f"{new_programs} New programs.",
            f"{hackerone_programs} Hackerone programs.",
            f"{bugcrowd_programs} Bugcrowd programs.",
            f"{yeswehacker_programs} Yeswehack programs.",
            f"{self_hosted_programs} Self hosted programs.",
            f"{programs_with_rewards} Programs with rewards.",
            f"{programs_with_swag} Programs offer swags.",
            f"{programs_with_no_rewards} No rewards programs.",
            "Back to Main Menu"
        ]

        questionary.select(
            "Info Menu",
            choices=info_options,
            style=self.style
        ).ask()

    def action_export(self):
        try:
            programme_names = sorted([p["name"] for p in self.data_json])
            selected = questionary.checkbox(
                "Select programs to export (Space to select, Enter to confirm):",
                choices=programme_names,
                style=self.style
            ).ask()

            if selected:
                for prog in selected:
                    with self.db_lock:
                        self.cursor.execute("SELECT ID FROM names WHERE name=?", (prog,))
                        row = self.cursor.fetchone()
                        subdomains = []
                        if row:
                            program_id = row[0]
                            self.cursor.execute("SELECT subdomain FROM subdomains WHERE program_ID=?", (program_id,))
                            subdomains = [r[0] for r in self.cursor.fetchall()]

                    if subdomains:
                        with open(f"{prog}_exported.txt", "w", encoding="utf-8") as file:
                            for sd in subdomains:
                                file.write(sd + "\n")
                        print(f"Exported {prog} to {prog}_exported.txt")
                    else:
                        print(f"No subdomains found for {prog} in DB.")
        except Exception as e:
            print(f"Export error: {e}")

    def main_menu(self):
        title = """
          _____ _                       _____                      _                 _           
         / ____| |                     |  __ \\                    | |               | |          
        | |    | |__   __ _  ___  ___  | |  | | _____      ___ __ | | ___   __ _  __| | ___ _ __ 
        | |    | '_ \\ / _` |/ _ \\/ __| | |  | |/ _ \\ \\ /\\ / / '_ \\| |/ _ \\ / _` |/ _` |/ _ \\ '__|
        | |____| | | | (_| | (_) \\__ \\ | |__| | (_) \\ V  V /| | | | | (_) | (_| | (_| |  __/ |   
         \\_____|_| |_|\\__,_|\\___/|___/ |_____/ \\___/ \\_/\\_/ |_| |_|_|\\___/ \\__,_|\\__,_|\\___|_|   

        This tool is designed to deal with chaos API from projectdiscovery.io
                    https://chaos.projectdiscovery.io/
        """
        print(self.Red + title + self.Reset)

        choices = [
            "All Programmes",
            "Offer Bounty",
            "Not Offer Bounty",
            "Platform",
            "New Subdomain",
            "New Subdomain and Offer Bounty",
            "New Subdomain and Offer Bounty and Platform",
            "New Subdomain and Platform",
            "New Subdomain and Not Offer Bounty",
            "New Subdomain and Not Offer Bounty and Platform",
            "Offer Bounty and Platform",
            "Not Offer Bounty and Platform",
            "Specific Programs",
            "Info about Programs",
            "Export Programme from Database",
            "Quit"
        ]

        while True:
            choice = questionary.select(
                "Main Menu",
                choices=choices,
                style=self.style
            ).ask()

            if choice == "Quit":
                break

            # Map choices to functions
            actions = {
                "All Programmes": self.action_download_all,
                "Offer Bounty": self.action_offer_bounty,
                "Not Offer Bounty": self.action_not_offer_bounty,
                "Platform": self.action_platform,
                "New Subdomain": self.action_new_subdomain,
                "New Subdomain and Offer Bounty": self.action_new_subdomain_and_offer_bounty,
                "New Subdomain and Offer Bounty and Platform": self.action_new_subdomain_and_offer_bounty_and_platform,
                "New Subdomain and Platform": self.action_new_subdomain_and_platform,
                "New Subdomain and Not Offer Bounty": self.action_new_subdomain_and_not_offer_bounty,
                "New Subdomain and Not Offer Bounty and Platform": self.action_new_subdomain_and_not_offer_bounty_and_platform,
                "Offer Bounty and Platform": self.action_offer_bounty_and_platform,
                "Not Offer Bounty and Platform": self.action_not_offer_bounty_and_platform,
                "Specific Programs": self.action_specific_program,
                "Info about Programs": self.action_info,
                "Export Programme from Database": self.action_export,
            }

            action = actions.get(choice)
            if action:
                action()
                input("\nPress Enter to continue...")
                self.clear_screen()
                print(self.Red + title + self.Reset)

if __name__ == "__main__":
    app = ChaosDownloader()
    app.clear_screen()
    app.main_menu()
