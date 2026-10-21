/* SpaceMenu — space management actions in the main header. */
function SpaceMenu({ actor, onShareSpace, onRules, onSpaceSettings, onExit }) {
  const { useEffect, useRef, useState } = React;
  const [open, setOpen] = useState(false);
  const triggerRef = useRef(null);
  const dialogRef = useRef(null);
  const restoreFocusRef = useRef(true);
  const isAdmin = actor === 'admin';

  useEffect(() => {
    if (!open) return undefined;
    dialogRef.current?.querySelector('button')?.focus();
    const onKeyDown = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setOpen(false);
      }
      if (event.key !== 'Tab') return;
      const buttons = dialogRef.current?.querySelectorAll('button:not([disabled])');
      if (!buttons?.length) return;
      const first = buttons[0];
      const last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      if (restoreFocusRef.current) triggerRef.current?.focus();
      restoreFocusRef.current = true;
    };
  }, [open]);

  const select = callback => {
    restoreFocusRef.current = false;
    setOpen(false);
    callback?.();
  };

  return (
    <>
      <button ref={triggerRef} type="button" className="btn space-menu__trigger"
        aria-haspopup="dialog" aria-expanded={open} aria-controls="space-menu-dialog"
        onClick={() => setOpen(true)}>
        <Icon name="settings" size={16}/>
        <span>{isAdmin ? '管理' : '会话'}</span>
      </button>
      {open && (
        <div className="space-menu__layer">
          <div className="space-menu__scrim" onClick={() => setOpen(false)}/>
          <section ref={dialogRef} id="space-menu-dialog" className="space-menu__panel"
            role="dialog" aria-modal="true" aria-labelledby="space-menu-title">
            <div className="space-menu__handle" aria-hidden="true"/>
            <header className="space-menu__header">
              <div>
                <small>{isAdmin ? 'SPACE ACTIONS' : 'SESSION'}</small>
                <h2 id="space-menu-title">{isAdmin ? '管理空间' : '参与空间'}</h2>
              </div>
              <button type="button" className="btn space-menu__close" onClick={() => setOpen(false)} aria-label="关闭菜单">
                <Icon name="x" size={16}/>
              </button>
            </header>
            <div className="space-menu__items">
              {isAdmin && <>
                <button type="button" className="space-menu__item" onClick={() => select(onShareSpace)}>
                  <span className="space-menu__item-icon"><Icon name="link" size={18}/></span>
                  <span className="space-menu__item-copy"><strong>分享空间</strong><small>邀请其他人加入</small></span>
                  <Icon name="arrow" size={16}/>
                </button>
                <button type="button" className="space-menu__item" onClick={() => select(onRules)}>
                  <span className="space-menu__item-icon"><Icon name="list" size={18}/></span>
                  <span className="space-menu__item-copy"><strong>自动化规则</strong><small>查看和管理已保存的规则</small></span>
                  <Icon name="arrow" size={16}/>
                </button>
                <button type="button" className="space-menu__item" onClick={() => select(onSpaceSettings)}>
                  <span className="space-menu__item-icon"><Icon name="settings" size={18}/></span>
                  <span className="space-menu__item-copy"><strong>空间设置</strong><small>名称与设备</small></span>
                  <Icon name="arrow" size={16}/>
                </button>
              </>}
              <button type="button" className="space-menu__item space-menu__item--exit" onClick={() => select(onExit)}>
                <span className="space-menu__item-icon"><Icon name="x" size={17}/></span>
                <span className="space-menu__item-copy"><strong>{isAdmin ? '退出管理' : '结束体验'}</strong></span>
                <Icon name="arrow" size={16}/>
              </button>
            </div>
          </section>
        </div>
      )}
    </>
  );
}

window.SpaceMenu = SpaceMenu;
