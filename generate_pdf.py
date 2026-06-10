from markdown_pdf import MarkdownPdf, Section
import sys

def main():
    try:
        pdf = MarkdownPdf(toc_level=2)
        with open("Project_Analysis_Guide.md", "r", encoding="utf-8") as f:
            content = f.read()
        
        pdf.add_section(Section(content, toc=False))
        pdf.save("Apply_Nav_Project_Analysis.pdf")
        print("Successfully generated Apply_Nav_Project_Analysis.pdf")
    except Exception as e:
        print(f"Error generating PDF: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
