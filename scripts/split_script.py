import re
import os

def split_script(file_path, max_chars=4000):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    content = re.sub(r'^## .*\n', '', content)
    
    pattern = r'(\[.*?\]\[.*?\]\[.*?\]:)'
    parts = re.split(pattern, content)
    
    segments = []
    current_segment = ""
    
    i = 1
    while i < len(parts):
        speaker_tag = parts[i]
        line_text = parts[i+1]
        full_line = speaker_tag + line_text
        
        if len(current_segment) + len(full_line) > max_chars:
            if current_segment:
                segments.append(current_segment.strip())
            current_segment = full_line
        else:
            current_segment += full_line
        i += 2
    
    if current_segment:
        segments.append(current_segment.strip())
        
    return segments

if __name__ == "__main__":
    script_path = os.getenv('SCRIPT_PATH', '/home/ubuntu/podcast_script.md')
    output_dir = os.getenv('OUTPUT_DIR', '/home/ubuntu/')
    max_chars = int(os.getenv('MAX_CHARS', '4000'))

    segments = split_script(script_path, max_chars=max_chars)
    for idx, seg in enumerate(segments):
        output_file = os.path.join(output_dir, f'script_segment_{idx}.txt')
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(seg)
        print(f"Segment {idx} length: {len(seg)}")
