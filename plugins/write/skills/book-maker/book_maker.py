#!/usr/bin/env python3

import os
import tempfile
import argparse
import pypandoc
import sys
import json

def log_font_paths(fonts):
    """Log the paths of the font files."""
    for font in fonts:
        print(f"Font path: {font}")

def find_font_files(font_name, font_dir):
    """Find font files for a given font name in the base fonts directory."""
    font_files = []
    
    # Normalize the font name by removing spaces for comparison
    normalized_font_name = font_name.lower().replace(" ", "")
    
    if not os.path.isdir(font_dir):
        print(f"Warning: Font directory '{font_dir}' not found")
        return font_files
        
    for file in os.listdir(font_dir):
        if file.endswith('.ttf') or file.endswith('.otf'):
            # Normalize the file name by removing spaces for comparison
            normalized_file_name = file.lower().replace(" ", "")
            
            # Check if the normalized file name contains the normalized font name
            if normalized_font_name in normalized_file_name:
                font_path = os.path.join(font_dir, file)
                font_files.append(font_path)
    
    if not font_files:
        print(f"Warning: No font files found for '{font_name}'")
    
    return font_files

def warn_missing_fonts(fonts):
    """Print a warning for any missing font files."""
    missing_fonts = []
    for font in fonts:
        if not os.path.isfile(font):
            print(f"Warning: font file '{font}' not found or unreadable. Make sure all font files are in the base fonts directory.", file=sys.stderr)
            missing_fonts.append(font)
    
    return len(missing_fonts) == 0

def create_temp_file(content):
    """Create a temporary file with the given content."""
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    return path

def latex_safe_path(path):
    """Return a path formatted for LaTeX."""
    return os.path.abspath(path).replace('\\', '/')

def get_font_info_from_styles(styles_path=None):
    """Extract font information from styles.json if available.
    
    This function is used only for backward compatibility with existing styles.json files.
    It extracts just the font names to ensure proper font embedding.
    """
    if not styles_path or not os.path.isfile(styles_path):
        return None
        
    try:
        with open(styles_path, 'r') as f:
            styles = json.load(f)
            return styles
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return None
    
def convert_markdown_to_formats(input_md, base_name, latex_styles_path, epub_css_path, font_dir, font_info=None):
    """Convert Markdown to PDF and EPUB using pypandoc."""
    # Get the font name from font_info if available
    pdf_font = None
    if font_info and "Body Text" in font_info and font_info["Body Text"].get("font_name"):
        pdf_font = font_info["Body Text"]["font_name"]
    else:
        # Default if no font specified
        pdf_font = "Quivira"
    
    # Prepare font files for embedding based on the specified font
    font_files = []
    if pdf_font:
        # Find the font files in the base font directory
        font_files = find_font_files(pdf_font, font_dir)
    
    # PDF conversion
    pdf_args = [
        '--toc',
        '--standalone',
        '--pdf-engine=xelatex',
        '-V', 'documentclass=book',
        '-V', 'classoption=oneside',
        '-V', 'geometry:a4paper',
        '-V', 'geometry:margin=1in',
        '--include-in-header', latex_styles_path,
        '--shift-heading-level-by=-1',
        '--top-level-division=part',
    ]
    
    # Add variable substitutions for line spacing if needed from font_info
    if font_info and "Body Text" in font_info and font_info["Body Text"].get("line_spacing"):
        line_spacing = font_info["Body Text"]["line_spacing"]
        if line_spacing != "single":
            if "x" in line_spacing:
                factor = line_spacing.replace("x", "")
                pdf_args.extend(['-V', f'linespread={factor}'])
    
    pypandoc.convert_file(
        input_md,
        'pdf',
        outputfile=f"{base_name}.pdf",
        extra_args=pdf_args
    )

    # EPUB conversion
    epub_args = [
        '--toc',
        '--standalone',
        '--css', epub_css_path,
        '--shift-heading-level-by=-1',
        '--top-level-division=part',
        '--split-level=2',
    ]
    
    # Add font embedding arguments for EPUB
    # For EPUB, we need to embed the fonts specified in the font_info
    epub_font_files = []
    if font_info:
        # Get all unique fonts used in font_info
        epub_fonts = set()
        for style_name, style in font_info.items():
            if style.get("font_name"):
                epub_fonts.add(style.get("font_name"))
        
        # Find font files for each font in the base font directory
        for epub_font in epub_fonts:
            font_files_found = find_font_files(epub_font, font_dir)
            epub_font_files.extend(font_files_found)
    
    # If no specific fonts found, use the provided font files
    if not epub_font_files:
        epub_font_files = font_files
    
    # Add font embedding arguments
    for font_file in epub_font_files:
        if os.path.isfile(font_file):
            epub_args.append(f'--epub-embed-font={font_file}')
    
    pypandoc.convert_file(
        input_md,
        'epub',
        outputfile=f"{base_name}.epub",
        extra_args=epub_args
    )

def main():
    parser = argparse.ArgumentParser(description="Create PDF and EPUB from Markdown using static style files")
    parser.add_argument("input_md", help="Input Markdown file to be converted")
    parser.add_argument("output_base", nargs="?", help="Base name for output files (default: input file name without extension)")
    args = parser.parse_args()

    if not os.path.isfile(args.input_md):
        parser.error(f"Input file '{args.input_md}' not found.")

    base_name = args.output_base or os.path.splitext(os.path.basename(args.input_md))[0]

    script_dir = os.path.dirname(os.path.abspath(__file__))
    font_dir = os.path.join(script_dir, "fonts")
    font_dir = os.path.abspath(font_dir)

    # Get font information from styles.json if provided
    font_info = get_font_info_from_styles()
    
    # Determine which fonts to use based on font_info
    required_fonts = {"Symbola"}  # Ensure Symbola is included for Unicode characters
    font_files = []
    
    if font_info:
        for style_name, style in font_info.items():
            if style.get("font_name"):
                required_fonts.add(style.get("font_name"))
        
        # Find font files for each required font in the base font directory
        for font_name in required_fonts:
            font_files_found = find_font_files(font_name, font_dir)
            font_files.extend(font_files_found)
    
    # If no fonts specified in styles or no font files found
    if not required_fonts or not font_files:

        # List all files in the font directory
        font_files = [os.path.join(font_dir, f) for f in os.listdir(font_dir) if os.path.isfile(os.path.join(font_dir, f))]
    
    # Validate that we have font files for all required fonts
    all_fonts_found = warn_missing_fonts(font_files)
    if not font_files:
        parser.error(
            f"No font files found for the specified fonts: {', '.join(required_fonts)}. "
            "Please ensure the fonts are installed in the fonts directory or use --font-path to specify their location. "
            "Available fonts: Quivira, GoudyBookletter1911."
        )
    elif not all_fonts_found:
        parser.error(
            f"Some required font files for {', '.join(required_fonts)} were not found. "
            "Please ensure all required font files are available."
        )

    # Get paths to static style files
    latex_styles_path = os.path.join(script_dir, "latex_styles.tex")
    epub_css_path = os.path.join(script_dir, "epub_styles.css")
    
    # Verify that the static style files exist
    if not os.path.isfile(latex_styles_path):
        parser.error(f"LaTeX styles file '{latex_styles_path}' not found.")
    if not os.path.isfile(epub_css_path):
        parser.error(f"EPUB CSS file '{epub_css_path}' not found.")

    try:
        convert_markdown_to_formats(args.input_md, base_name, latex_styles_path, epub_css_path, font_dir, font_info)
    finally:
        pass

if __name__ == "__main__":
    main()
