/* SpaceMenu — space management actions in the main header. */
function SpaceMenu({ onShareSpace, onTraining, onSpaceSettings, onOrchestratorSettings, onPeople, onExit }) {
  const { useEffect, useRef, useState } = React;
  const [open, setOpen] = useState(false);
  const triggerRef = useRef(null);
  const dialogRef = useRef(null);
  const restoreFocusRef = useRef(true);

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
      <button ref={triggerRef} type="button" className="icon-button space-menu__trigger" aria-label="管理" title="管理"
        aria-haspopup="dialog" aria-expanded={open} aria-controls="space-menu-dialog"
        onClick={() => setOpen(true)}>
        <Icon name="settings" size={16}/>
      </button>
      {open && (
        <div className="space-menu__layer">
          <div className="space-menu__scrim" onClick={() => setOpen(false)}/>
          <section ref={dialogRef} id="space-menu-dialog" className="management-dialog space-menu__panel"
            role="dialog" aria-modal="true" aria-labelledby="space-menu-title">
            <header className="space-menu__header">
              <div>
                <h2 id="space-menu-title">管理</h2>
              </div>
              <button type="button" className="icon-button space-menu__close" onClick={() => setOpen(false)} aria-label="关闭管理">
                <Icon name="x" size={16}/>
              </button>
            </header>
            <div className="space-menu__items">
              <button type="button" className="space-menu__item" onClick={() => select(onShareSpace)}>
                <span className="space-menu__item-icon"><Icon name="link" size={18}/></span>
                <span className="space-menu__item-copy"><strong>分享空间</strong><small>邀请其他人加入</small></span>
                <Icon name="arrow" size={16}/>
              </button>
              <button type="button" className="space-menu__item" onClick={() => select(onTraining)}>
                <span className="space-menu__item-icon"><Icon name="list" size={18}/></span>
                <span className="space-menu__item-copy"><strong>伙伴训练</strong><small>查看、调整或暂停已保存的训练</small></span>
                <Icon name="arrow" size={16}/>
              </button>
              <button type="button" className="space-menu__item" onClick={() => select(onSpaceSettings)}>
                <span className="space-menu__item-icon"><Icon name="settings" size={18}/></span>
                <span className="space-menu__item-copy"><strong>空间管理</strong><small>名称与设备</small></span>
                <Icon name="arrow" size={16}/>
              </button>
              <button type="button" className="space-menu__item" onClick={() => select(onOrchestratorSettings)}>
                <span className="space-menu__item-icon"><Icon name="sparkles" size={18}/></span>
                <span className="space-menu__item-copy"><strong>空间管家</strong><small>人格与特质</small></span>
                <Icon name="arrow" size={16}/>
              </button>
              <button type="button" className="space-menu__item" onClick={() => select(onPeople)}>
                <span className="space-menu__item-icon"><Icon name="user" size={18}/></span>
                <span className="space-menu__item-copy"><strong>人员库</strong><small>管理空间中的人员与照片</small></span>
                <Icon name="arrow" size={16}/>
              </button>
              <button type="button" className="space-menu__item space-menu__item--exit" onClick={() => select(onExit)}>
                <span className="space-menu__item-icon"><Icon name="logout" size={18}/></span>
                <span className="space-menu__item-copy"><strong>退出管理</strong></span>
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
