((python-base-mode
  . ((eval . (ruff-format-on-save-mode))
     (init/inhibit-buffer-formatting t)
     (eglot-workspace-configuration
      . ((:pylsp . (:plugins (:pycodestyle (:enabled :json-false)
                                           :autopep8 (:enabled :json-false)
                                           :flake8 (:enabled :json-false)
                                           ))))))))
