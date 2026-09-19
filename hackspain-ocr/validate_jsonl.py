# Validador de entrega: comprueba que outcomes.jsonl tiene {file_id, result} por cada PDF.
# Uso: python validate_jsonl.py outputs/outcomes.jsonl --pdf-dir data/facturas

import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl_path")
    parser.add_argument("--pdf-dir", required=True)
    args = parser.parse_args()
    # TODO: cargar jsonl, verificar file_id unicos, result en {PAGAR, NO_PAGAR, ESCALAR}
    print(f"TODO: validate {args.jsonl_path} against {args.pdf_dir}")


if __name__ == "__main__":
    main()
