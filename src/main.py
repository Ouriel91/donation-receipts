import argparse


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--mode", required=True)
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    print("Donation Receipts Project")
    print(f"Mode: {args.mode}")
    print(f"Dry run: {args.dry_run}")


if __name__ == "__main__":
    main()