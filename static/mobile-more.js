(function () {
    const sheet = document.getElementById('more-sheet');
    if (!sheet) return;

    function open(e) {
        if (e) e.preventDefault();
        sheet.hidden = false;
    }

    function close() {
        sheet.hidden = true;
    }

    document.querySelectorAll('[data-open="more-sheet"]').forEach(btn => {
        btn.addEventListener('click', open);
    });

    sheet.addEventListener('click', e => {
        if (e.target === sheet) close();
    });

    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !sheet.hidden) close();
    });
})();
