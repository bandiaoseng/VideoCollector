"""Parse the user's review separately from a platform's share caption."""
import re


def parse_note(note: str, categories: dict) -> tuple[str, str]:
    tags = list(re.finditer(r'(?<!\S)[#＃]([^\s#＃]+)', note))
    if not tags:
        raise ValueError('请在点评末尾加上 #类目，例如：这个开头值得借鉴 #营销技巧')
    if len(tags) != 1:
        raise ValueError('一次请指定一个 #类目，避免存错文件夹')
    tag = tags[0]
    category = tag.group(1)
    if not isinstance(categories.get(category), str) or not categories[category].strip():
        raise ValueError(f'类目“{category}”尚未绑定夸克文件夹，请先绑定或修改标签')
    comment = (note[:tag.start()].rstrip() + ' ' + note[tag.end():].lstrip()).strip()
    if not comment:
        raise ValueError('请补充点评文字，#类目 不作为视频文件名')
    return comment, category
