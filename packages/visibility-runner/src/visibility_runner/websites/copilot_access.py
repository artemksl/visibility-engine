"""Read-only Copilot obstruction detection, scoped outside conversation content."""
from __future__ import annotations


async def tooltip_only_dialog(item, *, deadline=None):
    """Recognize a modal wrapper whose entire rendered content is one tooltip."""
    from ..browser.operations import _await_call
    evaluate = getattr(item, "evaluate", None)
    if not callable(evaluate):
        return False
    return bool(await _await_call(evaluate, """root => {
        const rendered = element => {
            const box = element.getBoundingClientRect(), css = getComputedStyle(element);
            return box.width > 0 && box.height > 0 && css.display !== 'none'
                && css.visibility !== 'hidden' && css.visibility !== 'collapse';
        };
        const tooltips = Array.from(root.querySelectorAll('[role="tooltip"]')).filter(rendered);
        if (tooltips.length !== 1) return false;
        const tooltip = tooltips[0];
        const controls = root.querySelectorAll('button, a[href], input, textarea, select, '
            + '[role="button"], [role="link"], [role="textbox"], [contenteditable="true"], '
            + 'iframe, img, svg, video, canvas');
        if (Array.from(controls).some(node => rendered(node) && !tooltip.contains(node))) return false;
        const texts = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        for (let node = texts.nextNode(); node; node = texts.nextNode()) {
            if (!node.textContent.trim() || tooltip.contains(node) || !rendered(node.parentElement)) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            if (Array.from(range.getClientRects()).some(box => box.width > 0 && box.height > 0)) return false;
        }
        return true;
    }""", _deadline=deadline))


async def obstructed_composer(page, spec, *, deadline=None):
    from ..browser.operations import _await_call
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return False
    return bool(await _await_call(evaluate, """selectors => {
        const transcript='[data-testid="markdown-reply"], [data-testid="chatOutput"], '
            +'[data-content="ai-message"], [data-content="user-message"], [data-testid="user-message"]';
        for(const selector of selectors) for(const editor of document.querySelectorAll(selector)) {
            const rect=editor.getBoundingClientRect(), style=getComputedStyle(editor);
            if(!rect.width || !rect.height || style.visibility==='hidden' || style.display==='none') continue;
            const x=rect.left+rect.width/2, y=rect.top+rect.height/2;
            if(x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
            const top=document.elementFromPoint(x,y);
            if(!top || top===editor || editor.contains(top) || top.contains(editor) || top.closest(transcript)) continue;
            for(let node=top; node && node!==document.body; node=node.parentElement) {
                const css=getComputedStyle(node), box=node.getBoundingClientRect();
                if(css.pointerEvents!=='none' && ['fixed','absolute'].includes(css.position)
                    && Number(css.zIndex)>0 && box.width>=rect.width/2 && box.height>=rect.height) return true;
            }
        }
        return false;
    }""", list(spec.composer_selectors), _deadline=deadline))
