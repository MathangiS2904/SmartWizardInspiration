"""
Azure Blob Storage Service for Smart Wizard Templates
Provides functions and CLI commands to connect, list, search, download, and load
floorplan templates from Azure Blob Storage directly into memory or local storage.
"""

import os
import sys
import json
import argparse
from typing import List, Dict, Any, Optional
from datetime import datetime
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient, ContainerClient

# Load environment variables from .env
load_dotenv()

DEFAULT_CONN_STR = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
DEFAULT_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "prod-smartwizardtemplates")


class SmartWizardBlobManager:
    """Manager for Smart Wizard floorplan templates in Azure Blob Storage."""

    def __init__(self, connection_string: Optional[str] = None, container_name: Optional[str] = None):
        self.connection_string = connection_string or DEFAULT_CONN_STR
        self.container_name = container_name or DEFAULT_CONTAINER
        if self.connection_string:
            self.blob_service_client = BlobServiceClient.from_connection_string(self.connection_string)
            self.container_client = self.blob_service_client.get_container_client(self.container_name)
        else:
            self.blob_service_client = None
            self.container_client = None

    def list_floorplans(
        self,
        category: Optional[str] = None,
        keyword: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Lists all JSON floorplans in the container.
        Args:
            category: Optional BHK filter, e.g. '1BHK', '2BHK', '3BHK'
            keyword: Optional case-insensitive search term in blob path
        Returns:
            List of dictionaries with blob details
        """
        blobs = self.container_client.list_blobs()
        results = []

        for b in blobs:
            if not b.name.lower().endswith(".json"):
                continue

            parts = b.name.split("/")
            bhk_category = parts[0] if len(parts) > 1 else "Root"

            if category and category.lower() not in bhk_category.lower():
                continue

            if keyword and keyword.lower() not in b.name.lower():
                continue

            results.append({
                "name": b.name,
                "category": bhk_category,
                "filename": parts[-1],
                "size_kb": round(b.size / 1024, 2),
                "last_modified": b.last_modified.strftime("%Y-%m-%d %H:%M:%S") if b.last_modified else "N/A",
                "url": self.container_client.get_blob_client(b.name).url
            })

        results.sort(key=lambda x: x["name"])
        return results

    def get_blob_url(self, identifier: str) -> str:
        """Returns the URL of the blob."""
        blob_name = self.find_blob(identifier)
        return self.container_client.get_blob_client(blob_name).url

    def find_blob(self, identifier: str) -> str:
        """
        Resolves a blob name from an exact name, index, or partial search string.
        Raises ValueError if not found or ambiguous.
        """
        all_blobs = self.list_floorplans()

        # 1. Check if identifier is an integer index (1-based)
        if identifier.isdigit():
            idx = int(identifier) - 1
            if 0 <= idx < len(all_blobs):
                return all_blobs[idx]["name"]
            raise ValueError(f"Index {identifier} is out of range. Total templates: {len(all_blobs)}")

        # 2. Check exact match
        for b in all_blobs:
            if b["name"] == identifier or b["filename"] == identifier:
                return b["name"]

        # 3. Check case-insensitive substring match
        matches = [b["name"] for b in all_blobs if identifier.lower() in b["name"].lower()]
        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            matching_list = "\n  - ".join(matches[:10])
            raise ValueError(
                f"Identifier '{identifier}' is ambiguous. Found {len(matches)} matches:\n  - {matching_list}"
                + ("\n  ... and more" if len(matches) > 10 else "")
            )
        else:
            raise ValueError(f"No floorplan found matching '{identifier}' in container '{self.container_name}'.")

    def get_floorplan_json(self, blob_name_or_keyword: str) -> Dict[str, Any]:
        """
        Fetches the floorplan JSON directly from Azure Blob Storage into memory.
        """
        blob_name = self.find_blob(blob_name_or_keyword)
        blob_client = self.container_client.get_blob_client(blob_name)
        stream = blob_client.download_blob()
        raw_data = stream.readall()
        return json.loads(raw_data.decode("utf-8"))

    def download_floorplan(self, blob_name_or_keyword: str, dest_path: Optional[str] = None) -> str:
        """
        Downloads a floorplan JSON to a local file.
        Returns the destination file path.
        """
        blob_name = self.find_blob(blob_name_or_keyword)
        if not dest_path:
            filename = os.path.basename(blob_name)
            dest_path = filename

        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        blob_client = self.container_client.get_blob_client(blob_name)

        with open(dest_path, "wb") as f:
            download_stream = blob_client.download_blob()
            f.write(download_stream.readall())

        return dest_path

    def sync_all_floorplans(
        self,
        dest_dir: str = "cloud_templates",
        category: Optional[str] = None
    ) -> List[str]:
        """
        Batch downloads floorplan JSONs to a local directory while preserving folder hierarchy.
        """
        blobs = self.list_floorplans(category=category)
        downloaded = []
        os.makedirs(dest_dir, exist_ok=True)

        for i, b in enumerate(blobs, 1):
            local_file_path = os.path.join(dest_dir, b["name"].replace("/", os.sep))
            os.makedirs(os.path.dirname(local_file_path), exist_ok=True)
            blob_client = self.container_client.get_blob_client(b["name"])
            with open(local_file_path, "wb") as f:
                f.write(blob_client.download_blob().readall())
            downloaded.append(local_file_path)
            print(f"[{i}/{len(blobs)}] Downloaded: {b['name']}")

        return downloaded


AVAILABLE_CONTAINERS: List[str] = [
    "prod-smartwizardtemplates",
    "dev-smartwizardtemplates"
]

# Manager instances cache by container name
_managers: Dict[str, SmartWizardBlobManager] = {}

def get_blob_manager(container_name: Optional[str] = None) -> SmartWizardBlobManager:
    c_name = container_name or DEFAULT_CONTAINER
    if c_name not in _managers:
        _managers[c_name] = SmartWizardBlobManager(container_name=c_name)
    return _managers[c_name]


def main():
    parser = argparse.ArgumentParser(
        description="Azure Blob Storage Manager for Smart Wizard Floorplan Templates"
    )
    parser.add_argument("--list", action="store_true", help="List all floorplan templates in the container")
    parser.add_argument("--bhk", help="Filter by BHK category (e.g. 1BHK, 2BHK, 3BHK, 4BHK)")
    parser.add_argument("--search", "-s", help="Search for floorplans matching a keyword")
    parser.add_argument("--get", "-g", help="Download a floorplan by name, keyword, or list index")
    parser.add_argument("-o", "--output", help="Destination path for downloaded file")
    parser.add_argument("--sync-all", action="store_true", help="Download all floorplans locally")
    parser.add_argument("--dest-dir", default="cloud_templates", help="Target folder for sync-all (default: 'cloud_templates')")

    args = parser.parse_args()
    manager = get_blob_manager()

    if args.list or args.search or args.bhk:
        templates = manager.list_floorplans(category=args.bhk, keyword=args.search)
        print("=" * 85)
        print(f" AZURE BLOB TEMPLATES: Container '{manager.container_name}' ({len(templates)} found)")
        print("=" * 85)
        print(f"{'#':<4} {'Category':<10} {'Size':<10} {'Blob Path'}")
        print("-" * 85)
        for i, t in enumerate(templates, 1):
            print(f"{i:<4} {t['category']:<10} {t['size_kb']:>6.1f} KB  {t['name']}")
        print("=" * 85)
        print(f"Total: {len(templates)} templates.")
        print("Tip: Use --get <number> or --get '<name>' to download any template.")
        return

    if args.get:
        out_path = manager.download_floorplan(args.get, dest_path=args.output)
        print(f"[+] Successfully downloaded floorplan to: {out_path}")
        return

    if args.sync_all:
        print(f"[*] Syncing templates to '{args.dest_dir}'...")
        downloaded = manager.sync_all_floorplans(dest_dir=args.dest_dir, category=args.bhk)
        print(f"[+] Finished! Downloaded {len(downloaded)} templates to '{args.dest_dir}'.")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
