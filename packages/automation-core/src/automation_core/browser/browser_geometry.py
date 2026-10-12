"""Fixed browser scripts for the conservative main-frame geometry adapter.

No page content, form values or application storage is read. The private WeakMap
and counters are ephemeral observation metadata, destroyed with the document.
The audit rejects overlapping foreign rectangles rather than pretending that a
few sampled hit tests certify an arbitrary visible polygon.
"""

GEOMETRY_STATE = r"""(key) => {
    let state = window[key];
    if (!state) {
        state = {layout: 0, next: 1, nodes: new WeakMap()};
        state.observer = new MutationObserver(() => state.layout++);
        state.observer.observe(document,
            {subtree:true, childList:true, attributes:true, characterData:true});
        window.addEventListener('resize', () => state.layout++);
        window.addEventListener('scroll', () => state.layout++, true);
        Object.defineProperty(window, key, {value:state, configurable:true});
    }
    if (state.observer.takeRecords().length) state.layout++;
    return state.layout;
}"""

POINTER_GEOMETRY = r"""(element, spec) => {
    const state = window[spec.key];
    if (state.observer.takeRecords().length) state.layout++;
    if (!state.nodes.has(element)) state.nodes.set(element, state.next++);
    const width = innerWidth, height = innerHeight;
    const r = element.getBoundingClientRect(), style = getComputedStyle(element);
    const left = r.left + parseFloat(style.borderLeftWidth);
    const top = r.top + parseFloat(style.borderTopWidth);
    const right = r.right - parseFloat(style.borderRightWidth);
    const bottom = r.bottom - parseFloat(style.borderBottomWidth);
    const result = {kind:'certified', width, height, layout:state.layout,
        node:state.nodes.get(element), rect:[left,top,right-left,bottom-top],
        origin:[left,top], hit:true};
    const stop = kind => {result.kind=kind; return result;};
    if (!element.isConnected || element.ownerDocument !== document
        || element.getRootNode() !== document || window !== window.top
        || element.namespaceURI !== 'http://www.w3.org/1999/xhtml') return stop('unsupported');
    if (style.visibility !== 'visible' || style.display === 'none'
        || style.pointerEvents === 'none' || element.matches(':disabled')
        || right <= left || bottom <= top) return stop('blocked');
    if (left < 0 || top < 0 || right > document.documentElement.clientWidth
        || bottom > document.documentElement.clientHeight) return stop('unsupported');
    if (element.getClientRects().length !== 1 || document.getAnimations().length
        || (visualViewport && (visualViewport.scale !== 1
            || visualViewport.offsetLeft !== 0 || visualViewport.offsetTop !== 0))) return stop('unsupported');
    const nodes = document.querySelectorAll('*');
    if (nodes.length > 2048) return stop('unsupported');
    const intersects = b => b.width > 0 && b.height > 0
        && b.left < right && b.right > left && b.top < bottom && b.bottom > top;
    const generated = (node,pseudo) => {
        const content = getComputedStyle(node,pseudo).content;
        return content !== 'none' && content !== 'normal';
    };
    for (const node of nodes) {
        // Generated boxes and shadow trees can paint outside their host bbox.
        if (node.shadowRoot || generated(node,'::before') || generated(node,'::after'))
            return stop('unsupported');
        const s = getComputedStyle(node), related = node.contains(element) || element.contains(node);
        const boxes = [...node.getClientRects()];
        if (related) {
            if (s.transform !== 'none' || s.perspective !== 'none'
                || s.translate !== 'none' || s.rotate !== 'none' || s.scale !== 'none'
                || (s.zoom !== '1' && s.zoom !== 'normal') || s.clipPath !== 'none'
                || s.clip !== 'auto' || s.maskImage !== 'none'
                || s.borderTopLeftRadius !== '0px' || s.borderTopRightRadius !== '0px'
                || s.borderBottomLeftRadius !== '0px' || s.borderBottomRightRadius !== '0px'
                || node.namespaceURI !== 'http://www.w3.org/1999/xhtml') return stop('unsupported');
            if (node.contains(element) && node !== element
                && (s.overflowX !== 'visible' || s.overflowY !== 'visible')) {
                const b = node.getBoundingClientRect();
                if (left < b.left + node.clientLeft || top < b.top + node.clientTop
                    || right > b.left + node.clientLeft + node.clientWidth
                    || bottom > b.top + node.clientTop + node.clientHeight) return stop('unsupported');
            }
        } else if (s.visibility === 'visible' && s.display !== 'none'
                   && s.pointerEvents !== 'none' && boxes.some(intersects)) {
            // This deliberately rejects foreign boxes even when painted behind.
            return stop('blocked');
        }
    }
    const hit = (x,y) => {
        const node = document.elementFromPoint(x,y);
        return node === element || (node !== null && element.contains(node));
    };
    for (const x of [left+.25,(left+right)/2,right-.25])
        for (const y of [top+.25,(top+bottom)/2,bottom-.25])
            if (!hit(x,y)) return stop('blocked');
    if (spec.endpoint) {
        const [x,y] = spec.endpoint;
        result.hit = x >= left && x < right && y >= top && y < bottom && hit(x,y);
        if (!result.hit) return stop('blocked');
    }
    return result;
}"""

SCROLL_LAYOUT = r"""(element, spec) => {
    const scopes = spec.scope ? document.querySelectorAll(spec.scope) : [document.scrollingElement];
    const scope = scopes.length === 1 ? scopes[0] : null;
    if (!scope || scope.ownerDocument !== document) return null;
    if (window !== window.top || scope.namespaceURI !== 'http://www.w3.org/1999/xhtml'
        || scope.getRootNode() !== document || getComputedStyle(scope).direction !== 'ltr') return null;
    for (let node=scope; node; node=node.parentElement) {
        const s=getComputedStyle(node);
        if (s.transform !== 'none' || s.perspective !== 'none'
            || s.translate !== 'none' || s.rotate !== 'none' || s.scale !== 'none'
            || (s.zoom !== '1' && s.zoom !== 'normal')) return null;
    }
    const documentScope = scope === document.scrollingElement;
    const r = spec.has_target ? element.getBoundingClientRect() : null;
    const s = spec.has_target ? getComputedStyle(element) : null;
    const visible = !r || (s.visibility === 'visible' && s.display !== 'none'
        && r.width > 0 && r.height > 0);
    const rect = r ? [r.left,r.top,r.width,r.height] : null;
    const box = documentScope ? {left:0,top:0,right:innerWidth,bottom:innerHeight}
                             : scope.getBoundingClientRect();
    let inView = r ? visible && r.left >= Math.max(0,box.left)
        && r.top >= Math.max(0,box.top) && r.right <= Math.min(innerWidth,box.right)
        && r.bottom <= Math.min(innerHeight,box.bottom) : null;
    if (r) for (let node=element.parentElement; node; node=node.parentElement) {
        const s=getComputedStyle(node), b=node.getBoundingClientRect();
        if ((s.overflowX !== 'visible' && (r.left < b.left+node.clientLeft
            || r.right > b.left+node.clientLeft+node.clientWidth))
            || (s.overflowY !== 'visible' && (r.top < b.top+node.clientTop
            || r.bottom > b.top+node.clientTop+node.clientHeight))) inView=false;
    }
    return {x:scope.scrollLeft,y:scope.scrollTop,
        max_x:Math.max(0,scope.scrollWidth-scope.clientWidth),
        max_y:Math.max(0,scope.scrollHeight-scope.clientHeight),
        viewport_width:documentScope?innerWidth:scope.clientWidth,
        viewport_height:documentScope?innerHeight:scope.clientHeight,
        target_rect:rect,target_in_view:inView};
}"""

SCROLL_BY = r"""(element, spec) => {
    const scope = spec.document ? document.scrollingElement : element;
    scope.scrollBy({left:spec.dx,top:spec.dy,behavior:'instant'});
}"""

WHEEL_SCOPE = r"""(element, spec) => {
    const scope = spec.document ? document.scrollingElement : element;
    if (!scope) return false;
    if (!spec.viewport || spec.viewport[0] !== innerWidth || spec.viewport[1] !== innerHeight)
        return false;
    if (scope !== document.scrollingElement) {
        // Require the scope to consume the entire requested delta. Otherwise a
        // wheel can chain to an outer container despite a hit inside this scope.
        const s = getComputedStyle(scope), scrollable = ['auto','scroll','overlay'];
        if (spec.dx && (!scrollable.includes(s.overflowX)
            || scope.scrollLeft + spec.dx < 0
            || scope.scrollLeft + spec.dx > scope.scrollWidth - scope.clientWidth)) return false;
        if (spec.dy && (!scrollable.includes(s.overflowY)
            || scope.scrollTop + spec.dy < 0
            || scope.scrollTop + spec.dy > scope.scrollHeight - scope.clientHeight)) return false;
    }
    const hit = document.elementFromPoint(spec.x,spec.y);
    if (!hit || (scope !== document.scrollingElement && !scope.contains(hit))) return false;
    // Refuse routing through an intervening nested scrollable or scroll trap.
    for (let node=hit; node && node!==scope; node=node.parentElement) {
        const s=getComputedStyle(node);
        if ((spec.dx && node.scrollWidth>node.clientWidth && ['auto','scroll','overlay'].includes(s.overflowX))
            || (spec.dy && node.scrollHeight>node.clientHeight && ['auto','scroll','overlay'].includes(s.overflowY)))
            return false;
        if (s.overscrollBehaviorX !== 'auto' || s.overscrollBehaviorY !== 'auto') return false;
    }
    return true;
}"""
